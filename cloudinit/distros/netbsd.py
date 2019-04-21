# Copyright (C) 2014 Harm Weites
# Copyright (C) 2019 Gonéri Le Bouder
#
# This file is part of cloud-init. See LICENSE file for license information.

import crypt
import os
import six
from six import StringIO

import re

from cloudinit import distros
from cloudinit import helpers
from cloudinit import log as logging
from cloudinit import net
from cloudinit import ssh_util
from cloudinit import util
from cloudinit.distros import netbsd_util
from cloudinit.settings import PER_INSTANCE

from cloudinit.distros.parsers.sys_conf import SysConf

LOG = logging.getLogger(__name__)


class Distro(distros.Distro):
    hostname_conf_fn = '/etc/rc.conf'
    ci_sudoers_fn = '/usr/pkg/etc/sudoers.d/90-cloud-init-users'

    def __init__(self, name, cfg, paths):
        distros.Distro.__init__(self, name, cfg, paths)
        # This will be used to restrict certain
        # calls from repeatly happening (when they
        # should only happen say once per instance...)
        self._runner = helpers.Runners(paths)
        self.osfamily = 'netbsd'
        cfg['ssh_svcname'] = 'sshd'


    def _select_hostname(self, hostname, fqdn):
        if fqdn:
            return fqdn
        return hostname

    def _select_hostname(self, hostname, fqdn):
        return hostname

    def _read_system_hostname(self):
        sys_hostname = self._read_hostname(filename='/etc/rc.conf')
        return ('/etc/rc.conf', sys_hostname)

    def _read_hostname(self, filename, default=None):
        return netbsd_util.get_rc_config_value('hostname')

    def _write_hostname(self, hostname, filename):
        netbsd_util.set_rc_config_value('hostname', hostname, fn='/etc/rc.conf')

    def create_group(self, name, members):
        group_add_cmd = ['pw', '-n', name]
        if util.is_group(name):
            LOG.warning("Skipping creation of existing group '%s'", name)
        else:
            try:
                util.subp(group_add_cmd)
                LOG.info("Created new group %s", name)
            except Exception as e:
                util.logexc(LOG, "Failed to create group %s", name)
                raise e

        if len(members) > 0:
            for member in members:
                if not util.is_user(member):
                    LOG.warning("Unable to add group member '%s' to group '%s'"
                                "; user does not exist.", member, name)
                    continue
                try:
                    util.subp(['pw', 'usermod', '-n', name, '-G', member])
                    LOG.info("Added user '%s' to group '%s'", member, name)
                except Exception:
                    util.logexc(LOG, "Failed to add user '%s' to group '%s'",
                                member, name)

    def add_user(self, name, **kwargs):
        if util.is_user(name):
            LOG.info("User %s already exists, skipping.", name)
            return False

        adduser_cmd = ['useradd']
        log_adduser_cmd = ['useradd']

        adduser_opts = {
            "homedir": '-d',
            "gecos": '-c',
            "primary_group": '-g',
            "groups": '-G',
            "shell": '-s',
        }
        adduser_flags = {
            "no_user_group": '--no-user-group',
            "system": '--system',
            "no_log_init": '--no-log-init',
        }

        for key, val in kwargs.items():
            if (key in adduser_opts and val and
               isinstance(val, six.string_types)):
                adduser_cmd.extend([adduser_opts[key], val])

            elif key in adduser_flags and val:
                adduser_cmd.append(adduser_flags[key])
                log_adduser_cmd.append(adduser_flags[key])

        if not 'no_create_home' in kwargs or not 'system' in kwargs:
            adduser_cmd += ['-m']
            log_adduser_cmd += ['-m']

        adduser_cmd += [name]
        log_adduser_cmd += [name]

        # Run the command
        LOG.info("Adding user %s", name)
        try:
            util.subp(adduser_cmd, logstring=log_adduser_cmd)
        except Exception as e:
            util.logexc(LOG, "Failed to create user %s", name)
            raise e
        # Set the password if it is provided
        # For security consideration, only hashed passwd is assumed
        passwd_val = kwargs.get('passwd', None)
        if passwd_val is not None:
            self.set_passwd(name, passwd_val, hashed=True)

    def set_passwd(self, user, password, hashed=False):
        if hashed:
            hashed_pw = password
        else:
            hashed_pw = crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH))

        try:
            util.subp(['usermod', '-C', 'no', '-p', hashed_pw, user])
        except Exception as e:
            util.logexc(LOG, "Failed to set password for %s", user)
            raise e

    def force_passwd_change(self, user):
        try:
            util.subp(['usermod', '-F', name])
        except Exception as e:
            util.logexc(LOG, "Failed to set pw expiration for %s", user)
            raise e

    def lock_passwd(self, name):
        try:
            util.subp(['usermod', '-C', 'yes', name])
        except Exception as e:
            util.logexc(LOG, "Failed to lock user %s", name)
            raise e

    def create_user(self, name, **kwargs):
        self.add_user(name, **kwargs)

        # Set password if plain-text password provided and non-empty
        if 'plain_text_passwd' in kwargs and kwargs['plain_text_passwd']:
            self.set_passwd(name, kwargs['plain_text_passwd'])

        # Default locking down the account. 'lock_passwd' defaults to True.
        # lock account unless lock_password is False.
        if kwargs.get('lock_passwd', True):
            self.lock_passwd(name)

        # Configure sudo access
        if 'sudo' in kwargs and kwargs['sudo'] is not False:
            self.write_sudo_rules(name, kwargs['sudo'])

        # Import SSH keys
        if 'ssh_authorized_keys' in kwargs:
            keys = set(kwargs['ssh_authorized_keys']) or []
            ssh_util.setup_user_keys(keys, name, options=None)

    def generate_fallback_config(self):
        nconf = {'config': [], 'version': 1}
        for mac, name in net.get_interfaces_by_mac().items():
            nconf['config'].append(
                {'type': 'physical', 'name': name,
                 'mac_address': mac, 'subnets': [{'type': 'dhcp'}]})
        return nconf

    def _write_network_config(self, netconfig):
        return self._supported_write_network_config(netconfig)

    def apply_network_config_names(self, netconfig):
        # NetBSD cannot rename interfaces (and so simplify our life here)
        return

    def install_packages(self, pkglist):
        self.update_package_sources()
        self.package_command('install', pkgs=pkglist)

    def package_command(self, command, args=None, pkgs=None):
        if pkgs is None:
            pkgs = []

        os_release, _ = util.subp(['uname', '-r'])
        os_arch, _ = util.subp(['uname', '-m'])
        e = os.environ.copy()
        e['PKG_PATH'] = 'http://cdn.netbsd.org/pub/pkgsrc/packages/NetBSD/%s/%s/All/' % (os_arch, os_release)

        if command == 'install':
            cmd = ['pkg_add', '-U']
        elif command == 'remove':
            cmd = ['pkg_delete']
        if args and isinstance(args, str):
            cmd.append(args)
        elif args and isinstance(args, list):
            cmd.extend(args)

        pkglist = util.expand_package_list('%s-%s', pkgs)
        cmd.extend(pkglist)

        # Allow the output of this to flow outwards (ie not be captured)
        util.subp(cmd, env=e, capture=False)


    def apply_locale(self, locale, out_fn=None):
        pass

    def set_timezone(self, tz):
        distros.set_etc_timezone(tz=tz, tz_file=self._find_tz_file(tz))

    def update_package_sources(self):
        pass

    def user_passwords(self, entries, format):
        for user, password in entries:
            self.set_passwd(user, password, bool(format == 'hashed'))

    def user_passwords(self, entries, format):
        for user, password in entries:
            self.set_passwd(user, password, bool(format == 'hashed'))

# vi: ts=4 expandtab