from __future__ import print_function
import functools
import keystoneclient.v3
import keystoneauth1
import keystoneauth1.identity
import logging
import openstack
import os
import p9admin
import sys

class TooManyError(Exception):
    """Too many results found"""
    pass

def memoize(obj):
    cache = obj.cache = {}

    @functools.wraps(obj)
    def memoizer(*args):
        if args not in cache:
            cache[args] = obj(*args)
        return cache[args]
    return memoizer

def add_memo(obj, args, memo):
    obj.cache[args] = memo

class OpenStackClient(object):
    def __init__(self):
        self.logger = logging.getLogger(__name__)

        if os.environ.get("OS_PROTOCOL", "password") == "SAML":
            self.logger.info('Authenticating as "%s" on project "%s" with SAML',
                os.environ["OS_USERNAME"], os.environ["OS_PROJECT_NAME"])
            auth = self.saml().auth()
        else:
            self.logger.info('Authenticating as "%s" on project "%s" with password',
                os.environ["OS_USERNAME"], os.environ["OS_PROJECT_NAME"])
            auth = keystoneauth1.identity.v3.Password(
                auth_url=os.environ["OS_AUTH_URL"],
                username=os.environ["OS_USERNAME"],
                password=os.environ["OS_PASSWORD"],
                user_domain_id=os.environ["OS_USER_DOMAIN_ID"],
                project_name=os.environ["OS_PROJECT_NAME"],
                project_domain_id=os.environ["OS_PROJECT_DOMAIN_ID"],
            )

        self.session = keystoneauth1.session.Session(auth=auth)

    @memoize
    def keystone(self):
        return keystoneclient.v3.client.Client(session=self.session)

    @memoize
    def openstack(self):
        return openstack.connect(session=self.session)

    @memoize
    def saml(self):
        return p9admin.SAML(self)

    @memoize
    def role(self, name):
        return self.keystone().roles.find(name=name)

    @memoize
    def service_project(self):
        try:
            project = self.keystone().projects.find(name="service")
            self.logger.info('Found "%s" project [%s]', project.name, project.id)
        except keystoneauth1.exceptions.NotFound:
            self.logger.critical('Could not find project "service"')
            sys.exit(1)

        return project

    @memoize
    def external_network(self):
        name = "external"
        network = self.openstack().network.find_network(
            name, project_id=self.service_project().id)
        if network is None:
            self.logger.critical('Could not find network "%s" in project "%s"',
                name, self.service_project().name)
            sys.exit(1)

        return network

    @memoize
    def groups(self):
        groups = self.keystone().groups.list()
        self.logger.info('Retrieved %d groups', len(groups))
        return groups

    def subnets(self, *args, **kwargs):
        for subnet in self.openstack().network.subnets(*args, **kwargs):
            add_memo(self.subnet, (self, subnet.id), subnet)
            yield subnet

    @memoize
    def subnet(self, id):
        return self.openstack().network.get_subnet(id)

    def security_groups(self, *args, **kwargs):
        for sg in self.openstack().network.security_groups(*args, **kwargs):
            add_memo(self.security_group, (self, sg.id), sg)
            yield sg

    @memoize
    def security_group(self, id):
        return self.openstack().network.get_security_group(id)

    @memoize
    def all_volumes(self):
        return self.openstack().block_storage.volumes(details=True, all_tenants=True)

    def volumes(self, project_id):
        for volume in self.all_volumes():
            if volume.project_id == project_id:
                yield volume

    @memoize
    def all_servers(self):
        return self.openstack().compute.servers(details=True, all_tenants=True)

    def servers(self, project_id):
        for server in self.all_servers():
            if server.project_id == project_id:
                yield server

    def _find_user(self, email):
        try:
            return self.keystone().users.find(name=email)
        except keystoneauth1.exceptions.http.NotFound:
            return None

    def find_user(self, email):
        if isinstance(email, p9admin.User):
            user = email
            user.user = self._find_user(user.email)
            return user.user
        else:
            return self._find_user(email)

    def ensure_user(self, user, default_project=None):
        if user.user:
            return user.user

        user.user = self.find_user(user.email)
        if user.user is not None:
            self.logger.info('Found local user "%s" [%s]',
                user.user.name, user.user.id)
        else:
            user.user = self.keystone().users.create(
                name=user.email,
                email=user.email,
                description=user.name,
                default_project=default_project)
            self.logger.info('Created local user "%s" [%s]',
                user.user.name, user.user.id)
        return user.user

    def find_group(self, name):
        try:
            return self.keystone().groups.find(name=name)
        except keystoneauth1.exceptions.http.NotFound:
            return None

    def ensure_group(self, name):
        group = self.find_group(name)
        if group is not None:
            self.logger.info('Found group "%s" [%s]', group.name, group.id)
        else:
            group = self.keystone().groups.create(name=name)
            self.logger.info('Created group "%s" [%s]', group.name, group.id)
        return group

    def ensure_group_members(self, group, ensure_users, keep_others=False):
        existing_users = self.keystone().users.list(group=group)
        existing_users = set([u.id for u in existing_users])
        ensure_users = set([u.id for u in ensure_users])

        to_add = ensure_users - existing_users

        if keep_others:
            to_delete = set()
            unchanged = existing_users
        else:
            to_delete = existing_users - ensure_users
            unchanged = ensure_users & existing_users

        for user_id in to_add:
            self.keystone().users.add_to_group(user_id, group)
            self.logger.debug('Added user [%s] to group "%s" [%s]',
                user_id, group.name, group.id)

        for user_id in to_delete:
            self.keystone().users.remove_from_group(user_id, group)
            self.logger.debug('Deleted user [%s] from group "%s" [%s]',
                user_id, group.name, group.id)

        for user_id in unchanged:
            self.logger.debug('Leaving user [%s] in group "%s" [%s]',
                user_id, group.name, group.id)

        self.logger.info(
            'Updating group "%s" [%s] members: +%d -%d (%d unchanged)',
            group.name, group.id, len(to_add), len(to_delete), len(unchanged))

    def grant_project_access(self, project, user=None, group=None, role_name="_member_"):
        if user is None and group is not None:
            subject = 'group "{}"'.format(group.name)
        elif user is not None and group is None:
            subject = 'user "{}"'.format(user.name)
        else:
            raise ValueError("Must specify exactly one of user or group")

        role = self.role(role_name)
        if self.check_role_assignment(role.id, user=user, group=group, project=project):
            self.logger.info(
                'Found %s access to project "%s" with role "%s" [%s]',
                subject, project.name, role.name, role.id)
        else:
            self.keystone().roles.grant(role.id, user=user, group=group, project=project)
            self.logger.info(
                'Granted %s access to project "%s" with role "%s" [%s]',
                subject, project.name, role.name, role.id)

    def revoke_project_access(self, project, user=None, group=None, role_name="_member_"):
        if user is None and group is not None:
            subject = 'group "{}"'.format(group.name)
        elif user is not None and group is None:
            subject = 'user "{}"'.format(user.name)
        else:
            raise ValueError("Must specify exactly one of user or group")

        role = self.role(role_name)
        if self.check_role_assignment(role.id, user=user, group=group, project=project):
            self.keystone().roles.revoke(role.id, user=user, group=group, project=project)
            self.logger.info(
                'Revoked %s access to project "%s" with role "%s" [%s]',
                subject, project.name, role.name, role.id)
        else:
            self.logger.info(
                'No access for %s to project "%s" with role "%s" [%s]',
                subject, project.name, role.name, role.id)

    def check_role_assignment(self, role, **kwargs):
        try:
            if self.keystone().roles.check(role, **kwargs):
                return True
        except keystoneauth1.exceptions.http.NotFound:
            pass
        return False

    def find_network(self, project, name):
        networks = self.openstack().network.networks(project_id=project.id, name=name)
        for network in networks:
            self.logger.info('Found network "%s" [%s]', network.name, network.id)
            return network
        return None

    def create_network(self, project, name):
        network = self.openstack().network.create_network(
            project_id=project.id, name=name,
            description="Default network")
        self.logger.info('Created network "%s" [%s]', network.name, network.id)
        return network

    def find_subnet(self, project, network, name):
        subnets = self.openstack().network.subnets(
            project_id=project.id, network_id=network.id, name=name)
        for subnet in subnets:
            self.logger.info('Found subnet "%s" [%s]: %s',
                subnet.name, subnet.id, subnet.cidr)
            return subnet
        return None

    def create_subnet(self, project, network, name, cidr):
        subnet = self.openstack().network.create_subnet(
            project_id=project.id, network_id=network.id, name=name,
            ip_version=4, cidr=cidr, description="Default subnet")
        self.logger.info('Created subnet "%s" [%s]: %s',
            subnet.name, subnet.id, subnet.cidr)
        return subnet

    def find_router(self, project, name):
        routers = self.openstack().network.routers(project_id=project.id, name=name)
        for router in routers:
            self.logger.info('Found router "%s" [%s]', router.name, router.id)
            return router
        return None

    def create_router(self, project, network, subnet, name):
        router = self.openstack().network.create_router(
            project_id=project.id, name=name,
            description="Default router",
            external_gateway_info={"network_id": self.external_network().id})
        self.logger.info('Created router "%s" [%s]', router.name, router.id)

        port = self.openstack().network.create_port(
            project_id=project.id,
            network_id=network.id,
            fixed_ips=[
                {"subnet_id": subnet.id, "ip_address": subnet.gateway_ip}
            ])
        self.logger.info("Created port [%s] on tenant subnet", port.id)

        self.openstack().network.add_interface_to_router(
            router, subnet_id=subnet.id, port_id=port.id)
        self.logger.info("Added port to router")

        return router

    def find_security_group(self, project, name):
        security_groups = self.openstack().network.security_groups(
            project_id=project.id, name=name)
        for sg in security_groups:
            self.logger.info('Found security group "%s" [%s]', sg.name, sg.id)
            return sg
        return None

    def create_security_group(self, project, name):
        sg = self.openstack().network.create_security_group(
            name=name, project_id=project.id,
            description="Default security group")
        self.logger.info('Created security group "%s" [%s]', sg.name, sg.id)
        return sg

    def find_security_group_rule(self, security_group):
        sg_rules = self.openstack().network.security_group_rules(
            security_group_id=security_group.id,
            direction="ingress",
            ethertype="IPv4")
        for sg_rule in sg_rules:
            if sg_rule.remote_ip_prefix == "0.0.0.0/0":
                self.logger.info('Found security group rule for "%s" [%s]',
                    sg_rule.remote_ip_prefix, sg_rule.id)
                return sg_rule
        return None

    def create_security_group_rule(self, security_group):
        sg_rule = self.openstack().network.create_security_group_rule(
            security_group_id=security_group.id,
            direction="ingress",
            ethertype="IPv4",
            remote_ip_prefix="0.0.0.0/0")
        self.logger.info('Created security group rule for "%s" [%s]',
            sg_rule.remote_ip_prefix, sg_rule.id)
        return sg_rule

    def find_project(self, name):
        try:
            return self.keystone().projects.find(name=name)
        except keystoneauth1.exceptions.NotFound:
            # Maybe the name is an ID?
            try:
                return self.keystone().projects.get(name)
            except keystoneauth1.exceptions.http.NotFound:
                sys.exit('Could not find project with name or ID "%s"' % name)
