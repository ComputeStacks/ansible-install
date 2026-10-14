"""Pure decision logic for roles/node_kernel.

Everything in here is a function of its arguments: no command runs, nothing
is read off a host. tasks/main.yml registers `networkctl --json=short status
<uplink_interface>` and hands the result, the interface name and the host's
interface list to node_kernel_uplink_ra; tests/node_kernel_uplink_ra.yml
feeds it canned results to prove the rules without a network.
"""

import json
import os

# networkd AdministrativeState values that mean "a .network file matched this
# link and networkd owns it". `unmanaged` (no file matched, or networkd is
# told to leave the link alone) and `missing` (no such link) do not.
_MANAGED_STATES = ("pending", "initialized", "configuring", "configured",
                   "failed", "linger")


def _link_record(data, interface):
    """`status <if>` prints the link object itself; tolerate the list form."""
    if isinstance(data, dict) and isinstance(data.get("Interfaces"), list):
        for link in data["Interfaces"]:
            if isinstance(link, dict) and link.get("Name") == interface:
                return link
        return None
    if isinstance(data, dict):
        return data
    return None


def node_kernel_uplink_ra(result, interface, interfaces=None):
    """How to keep IPv6 router advertisements accepted on the uplink.

    `result` is a registered `networkctl --json=short status <interface>`
    (command module, failed_when false). `interfaces` is the host's
    `ansible_facts['interfaces']`, used only to tell a misspelled uplink_interface
    from a host without networkd.

    Returns {"mode": ..., "network_file": ..., "reason": ...}:

    * mode "networkd": systemd-networkd manages the link. networkd forces the
      kernel's accept_ra to 0 on links it manages and handles RAs in
      userspace, so the sysctl is useless there; the fix is a drop-in for
      `network_file` (the basename of the .network file networkd matched,
      e.g. 10-netplan-eth0.network) setting IPv6AcceptRA=yes.
    * mode "sysctl": the link exists but networkd does not manage it (or
      networkctl is absent or networkd not running); the kernel handles RAs,
      so net.ipv6.conf.<if>.accept_ra=2 is the fix.
    * mode "missing": the host has no such interface.
    """
    result = result or {}
    if interfaces is not None and interface not in interfaces:
        return {
            "mode": "missing",
            "network_file": "",
            "reason": "uplink_interface '%s' is not an interface on this host "
                      "(it has: %s)" % (interface, ", ".join(sorted(interfaces))),
        }

    if result.get("rc") != 0:
        return {
            "mode": "sysctl",
            "network_file": "",
            "reason": "networkctl status failed (rc %s): networkd is not "
                      "managing %s" % (result.get("rc"), interface),
        }

    try:
        data = json.loads(result.get("stdout") or "")
    except ValueError:
        return {
            "mode": "sysctl",
            "network_file": "",
            "reason": "networkctl printed no JSON for %s" % interface,
        }

    link = _link_record(data, interface)
    if link is None:
        return {
            "mode": "sysctl",
            "network_file": "",
            "reason": "networkctl does not list %s" % interface,
        }

    state = link.get("AdministrativeState") or ""
    network_file = link.get("NetworkFile") or ""
    if state not in _MANAGED_STATES or not network_file:
        return {
            "mode": "sysctl",
            "network_file": "",
            "reason": "systemd-networkd does not manage %s (state '%s')"
                      % (interface, state or "unknown"),
        }

    basename = os.path.basename(network_file)
    if not basename.endswith(".network") or basename == ".network":
        return {
            "mode": "sysctl",
            "network_file": "",
            "reason": "networkctl reported an unexpected network file '%s' "
                      "for %s" % (network_file, interface),
        }

    return {
        "mode": "networkd",
        "network_file": basename,
        "reason": "systemd-networkd manages %s with %s" % (interface, network_file),
    }


class FilterModule(object):
    def filters(self):
        return {"node_kernel_uplink_ra": node_kernel_uplink_ra}
