# This file is part of cloud-init. See LICENSE file for license information.

import os
import re

from cloudinit import log as logging
from cloudinit import util
from cloudinit.distros import rhel_util
from cloudinit.distros import netbsd_util
from cloudinit.distros.parsers.resolv_conf import ResolvConf

from . import renderer

LOG = logging.getLogger(__name__)


class Renderer(renderer.Renderer):
    resolv_conf_fn = '/etc/resolv.conf'

    def __init__(self, config=None):
        if not config:
            config = {}
        self.dhcp_interfaces = []
        self._postcmds = config.get('postcmds', True)

    def _render_route(self, route, indent=""):
        pass

    def _render_iface(self, iface, render_hwaddress=False):
        pass

    def _ifconfig_a(self):
        (out, _) = util.subp(['ifconfig', '-a'])
        return out

    def _get_ifname_by_mac(self, mac):
        out = self._ifconfig_a()
        blocks = re.split(r'(^\S+|\n\S+):', out)
        blocks.reverse()
        blocks.pop()  # Ignore the first one
        while blocks:
            ifname = blocks.pop()
            m = re.search(r'address:\s([\da-f:]{17})', blocks.pop())
            if m and m.group(1) == mac:
                return ifname

    def _write_network(self, settings):
        nameservers = []
        searchdomains = []
        for interface in settings.iter_interfaces():
            device_mac = interface.get("mac_address")
            device_name = interface.get("name")
            if device_mac:
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
                if subnet.get('gateway'):
                    netbsd_util.set_rc_config_value(
                        'defaultroute', subnet.get('gateway'))

                if 'dns_nameservers' in subnet:
                    nameservers.extend(subnet['dns_nameservers'])
                if 'dns_search' in subnet:
                    searchdomains.extend(subnet['dns_search'])
                netbsd_util.set_rc_config_value('ifconfig_' + device_name, ifconfig)
            else:
                self.dhcp_interfaces.append(device_name)


        if self.dhcp_interfaces:
            netbsd_util.set_rc_config_value('dhcpcd', 'YES')
            netbsd_util.set_rc_config_value('dhcpcd_flags', ' '.join(self.dhcp_interfaces))

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
        self.start_services()

    def render_network_state(self, network_state, templates=None, target=None):
        self._write_network(network_state)

    def start_services(self):
        if not self._postcmds:
            LOG.debug("netbsd generate postcmd disabled")
            return

        util.subp(['service', 'network', 'restart'], capture=True)
        if self.dhcp_interfaces:
            util.subp(['service', 'dhcpcd', 'restart'], capture=True)


def available(target=None):
    rcconf_path = util.target_path(target, 'etc/rc.conf')
    if not os.path.isfile(rcconf_path):
        return False

    return True
