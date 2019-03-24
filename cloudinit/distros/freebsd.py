# Copyright (C) 2014 Harm Weites
#
# Author: Harm Weites <harm@weites.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import os
import six
from six import StringIO

import re

from cloudinit import distros
from cloudinit import helpers
from cloudinit import log as logging
from cloudinit import ssh_util
from cloudinit import util

from cloudinit.distros import net_util
from cloudinit.distros.parsers.resolv_conf import ResolvConf

from cloudinit.settings import PER_INSTANCE

LOG = logging.getLogger(__name__)


class Distro(distros.Distro):
    rc_conf_fn = "/etc/rc.conf"
    login_conf_fn = '/etc/login.conf'
    login_conf_fn_bak = '/etc/login.conf.orig'
    resolv_conf_fn = '/etc/resolv.conf'
    ci_sudoers_fn = '/usr/local/etc/sudoers.d/90-cloud-init-users'
    default_primary_nic = 'hn0'

    def __init__(self, name, cfg, paths):
        distros.Distro.__init__(self, name, cfg, paths)
        # This will be used to restrict certain
        # calls from repeatly happening (when they
        # should only happen say once per instance...)
        self._runner = helpers.Runners(paths)
        self.osfamily = 'freebsd'
        self.ipv4_pat = re.compile(r"\s+inet\s+\d+[.]\d+[.]\d+[.]\d+")
        cfg['ssh_svcname'] = 'sshd'

    # Updates a key in /etc/rc.conf.
    def updatercconf(self, key, value):
        LOG.debug("Checking %s for: %s = %s", self.rc_conf_fn, key, value)
        conf = self.loadrcconf()
        config_changed = False
        if key not in conf:
            LOG.debug("Adding key in %s: %s = %s", self.rc_conf_fn, key,
                      value)
            conf[key] = value
            config_changed = True
        else:
            for item in conf.keys():
                if item == key and conf[item] != value:
                    conf[item] = value
                    LOG.debug("Changing key in %s: %s = %s", self.rc_conf_fn,
                              key, value)
                    config_changed = True

        if config_changed:
            LOG.info("Writing %s", self.rc_conf_fn)
            buf = StringIO()
            for keyval in conf.items():
                buf.write('%s="%s"\n' % keyval)
            util.write_file(self.rc_conf_fn, buf.getvalue())

    # Load the contents of /etc/rc.conf and store all keys in a dict. Make sure
    # quotes are ignored:
    #  hostname="bla"
    def loadrcconf(self):
        RE_MATCH = re.compile(r'^(\w+)\s*=\s*(.*)\s*')
        conf = {}
        lines = util.load_file(self.rc_conf_fn).splitlines()
        for line in lines:
            m = RE_MATCH.match(line)
            if not m:
                LOG.debug("Skipping line from /etc/rc.conf: %s", line)
                continue
            key = m.group(1).rstrip()
            val = m.group(2).rstrip()
            # Kill them quotes (not completely correct, aka won't handle
            # quoted values, but should be ok ...)
            if val[0] in ('"', "'"):
                val = val[1:]
            if val[-1] in ('"', "'"):
                val = val[0:-1]
            if len(val) == 0:
                LOG.debug("Skipping empty value from /etc/rc.conf: %s", line)
                continue
            conf[key] = val
        return conf

    def readrcconf(self, key):
        conf = self.loadrcconf()
        try:
            val = conf[key]
        except KeyError:
            val = None
        return val

    # NOVA will inject something like eth0, rewrite that to use the FreeBSD
    # adapter. Since this adapter is based on the used driver, we need to
    # figure out which interfaces are available. On KVM platforms this is
    # vtnet0, where Xen would use xn0.
    def getnetifname(self, dev):
        LOG.debug("Translating network interface %s", dev)
        if dev.startswith('lo'):
            return dev

        n = re.search(r'\d+$', dev)
        index = n.group(0)

        (out, _err) = util.subp(['ifconfig', '-a'])
        ifconfigoutput = [x for x in (out.strip()).splitlines()
                          if len(x.split()) > 0]
        bsddev = 'NOT_FOUND'
        for line in ifconfigoutput:
            m = re.match(r'^\w+', line)
            if m:
                if m.group(0).startswith('lo'):
                    continue
                # Just settle with the first non-lo adapter we find, since it's
                # rather unlikely there will be multiple nicdrivers involved.
                bsddev = m.group(0)
                break

        # Replace the index with the one we're after.
        bsddev = re.sub(r'\d+$', index, bsddev)
        LOG.debug("Using network interface %s", bsddev)
        return bsddev

    def _select_hostname(self, hostname, fqdn):
        # Should be FQDN if available. See rc.conf(5) in FreeBSD
        if fqdn:
            return fqdn
        return hostname

    def _read_system_hostname(self):
        sys_hostname = self._read_hostname(filename=None)
        return ('rc.conf', sys_hostname)

    def _read_hostname(self, filename, default=None):
        hostname = None
        try:
            hostname = self.readrcconf('hostname')
        except IOError:
            pass
        if not hostname:
            return default
        return hostname

    def _write_hostname(self, hostname, filename):
        self.updatercconf('hostname', hostname)

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

        adduser_cmd = ['pw', 'useradd', '-n', name]
        log_adduser_cmd = ['pw', 'useradd', '-n', name]

        adduser_opts = {
            "homedir": '-d',
            "gecos": '-c',
            "primary_group": '-g',
            "groups": '-G',
            "shell": '-s',
            "inactive": '-E',
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

        if 'no_create_home' in kwargs or 'system' in kwargs:
            adduser_cmd.append('-d/nonexistent')
            log_adduser_cmd.append('-d/nonexistent')
        else:
            adduser_cmd.append('-d/usr/home/%s' % name)
            adduser_cmd.append('-m')
            log_adduser_cmd.append('-d/usr/home/%s' % name)
            log_adduser_cmd.append('-m')

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

    def set_passwd(self, user, passwd, hashed=False):
        if hashed:
            hash_opt = "-H"
        else:
            hash_opt = "-h"

        try:
            util.subp(['pw', 'usermod', user, hash_opt, '0'],
                      data=passwd, logstring="chpasswd for %s" % user)
        except Exception as e:
            util.logexc(LOG, "Failed to set password for %s", user)
            raise e

    def lock_passwd(self, name):
        try:
            util.subp(['pw', 'usermod', name, '-h', '-'])
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

    def _write_network_config(self, netconfig):
        return self._supported_write_network_config(netconfig)

    def apply_locale(self, locale, out_fn=None):
        # Adjust the locals value to the new value
        newconf = StringIO()
        for line in util.load_file(self.login_conf_fn).splitlines():
            newconf.write(re.sub(r'^default:',
                                 r'default:lang=%s:' % locale, line))
            newconf.write("\n")

        # Make a backup of login.conf.
        util.copy(self.login_conf_fn, self.login_conf_fn_bak)

        # And write the new login.conf.
        util.write_file(self.login_conf_fn, newconf.getvalue())

        try:
            LOG.debug("Running cap_mkdb for %s", locale)
            util.subp(['cap_mkdb', self.login_conf_fn])
        except util.ProcessExecutionError:
            # cap_mkdb failed, so restore the backup.
            util.logexc(LOG, "Failed to apply locale %s", locale)
            try:
                util.copy(self.login_conf_fn_bak, self.login_conf_fn)
            except IOError:
                util.logexc(LOG, "Failed to restore %s backup",
                            self.login_conf_fn)

    def install_packages(self, pkglist):
        self.update_package_sources()
        self.package_command('install', pkgs=pkglist)

    def package_command(self, command, args=None, pkgs=None):
        if pkgs is None:
            pkgs = []

        e = os.environ.copy()
        e['ASSUME_ALWAYS_YES'] = 'YES'

        cmd = ['pkg']
        if args and isinstance(args, str):
            cmd.append(args)
        elif args and isinstance(args, list):
            cmd.extend(args)

        if command:
            cmd.append(command)

        pkglist = util.expand_package_list('%s-%s', pkgs)
        cmd.extend(pkglist)

        # Allow the output of this to flow outwards (ie not be captured)
        util.subp(cmd, env=e, capture=False)

    def set_timezone(self, tz):
        distros.set_etc_timezone(tz=tz, tz_file=self._find_tz_file(tz))

    def update_package_sources(self):
        self._runner.run("update-sources", self.package_command,
                         ["update"], freq=PER_INSTANCE)

# vi: ts=4 expandtab
