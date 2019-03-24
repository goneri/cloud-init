# This file is part of cloud-init. See LICENSE file for license information.

import os
import re
from six import StringIO

from cloudinit import log as logging
from cloudinit import util
from cloudinit.distros import net_util
from cloudinit.distros.parsers.resolv_conf import ResolvConf

LOG = logging.getLogger(__name__)

from . import renderer

class Renderer(renderer.Renderer):
    rc_conf_fn = "/etc/rc.conf"
    resolv_conf_fn = '/etc/resolv.conf'

    # Updates a key in /etc/rc.conf.
    # TODO(Goneri): Duplicated with distros.freebsd
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
    # TODO(Goneri): Duplicated with distros.freebsd
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

    # TODO(Goneri): Duplicated with distros.freebsd
    def readrcconf(self, key):
        conf = self.loadrcconf()
        try:
            val = conf[key]
        except KeyError:
            val = None
        return val

    def __init__(self, config=None):
        if not config:
            config = {}
        self.rcconf_path = config.get('rcconf_path', 'etc/rc.conf')

    def _render_route(self, route, indent=""):
        pass

    def _render_iface(self, iface, render_hwaddress=False):
        pass

    def _get_ifname_by_mac(self, mac):
        (out, _err) = util.subp(['ifconfig', '-a'])
        blocks = re.split('(^\S+|\n\S+):', out)
        blocks.reverse()
        blocks.pop()  # Ignore the first one
        while blocks:
            ifname = blocks.pop()
            m = re.search('ether\s([\da-f:]{17})', blocks.pop())
            if m and m.group(1) == mac:
                return ifname


    def _write_network(self, settings):
        entries = settings
        nameservers = []
        searchdomains = []
        for interface in settings.iter_interfaces():
            device_name = interface.get("name")
            device_mac = interface.get("mac_address")
            if device_name:
                if re.match('^lo\d+$', device_name):
                    continue
            if device_mac and device_name:
                cur_name = self._get_ifname_by_mac(device_mac)
                if cur_name != device_name:
                    self.updatercconf('ifconfig_%s_name' % cur_name, device_name)
            else:
                device_name = self._get_ifname_by_mac(device_mac)

            subnet = interface.get("subnets", [])[0]
            LOG.info('Configuring interface %s', device_name)

            if subnet.get('type') == 'static':
                LOG.debug('Configuring dev %s with %s / %s', device_name,
                          subnet.get('address'), subnet.get('netmask'))
                # Configure an ipv4 address.
                ifconfig = (subnet.get('address') + ' netmask ' +
                            subnet.get('netmask'))

                # Configure the gateway.
                self.updatercconf('defaultrouter', subnet.get('gateway'))

                if 'dns_nameservers' in subnet:
                    nameservers.extend(subnet['dns_nameservers'])
                if 'dns_search' in subnet:
                    searchdomains.extend(subnet['dns_search'])
            else:
                ifconfig = 'DHCP'

            self.updatercconf('ifconfig_' + device_name, ifconfig)
        # Note: We don't try to be cleaver beceause if an interface
        # is renamed, we must reload the netif.
        util.subp(['/etc/rc.d/netif', 'restart'])
        util.subp(['/etc/rc.d/routing', 'restart'])

        # Try to read the /etc/resolv.conf or just start from scratch if that
        # fails.
        try:
            resolvconf = ResolvConf(util.load_file(self.resolv_conf_fn))
            resolvconf.parse()
        except IOError:
            util.logexc(LOG, "Failed to parse %s, use new empty file",
                        self.resolv_conf_fn)
            resolvconf = ResolvConf('')
            resolvconf.parse()

        # Add some nameservers
        for server in nameservers:
            try:
                resolvconf.add_nameserver(server)
            except ValueError:
                util.logexc(LOG, "Failed to add nameserver %s", server)

        # And add any searchdomains.
        for domain in searchdomains:
            try:
                resolvconf.add_search_domain(domain)
            except ValueError:
                util.logexc(LOG, "Failed to add search domain %s", domain)
        util.write_file(self.resolv_conf_fn, str(resolvconf), 0o644)

    def render_network_state(self, network_state, templates=None, target=None):
        self._write_network(network_state)

def available(target=None):
    rcconf_path = util.target_path(target, 'etc/rc.conf')
    if not os.path.isfile(rcconf_path):
        return False

    return True
