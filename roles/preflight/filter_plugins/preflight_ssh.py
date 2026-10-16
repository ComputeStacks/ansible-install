"""Pure ssh-source checks for roles/preflight (tasks/ssh_source.yml).

A narrowed `firewall_allow_ssh_from` locks out every source it does not
cover the moment roles/firewall applies it, and the connection ansible is
using at that moment survives (`ct state established`), so nothing in the
run itself notices. These filters decide, before anything is changed,
whether the address this run reaches a host from is one that host's
firewall will still accept ssh from.

`preflight_ssh_connection` reads `SSH_CONNECTION` as sshd sets it on the
host. `preflight_ssh_covered` decides whether an address is covered by a
host's EFFECTIVE ssh accept set -- what templates/cs-static.nft.j2 in
roles/firewall renders for it, minus the paths that are not keyed on an
address (the tailnet interface accepts, raw `firewall_extra_input_rules`).
Leaving those out can only refuse a run the firewall would have let through,
never the other way round. tests/firewall_ssh_source.yml proves the cases and
that the filter and the template agree.

Python's `ipaddress` rather than ansible.utils' `ipaddr`: that collection is
not a dependency of this repository.
"""

import ipaddress

# Always accepted by the table (`iifname "lo" accept`). Only a run through a
# tunnel that ends on the host itself ever comes from here.
LOOPBACK = ("127.0.0.0/8", "::1/128")


def _address(value):
    """`value` as an ip_address, IPv4-mapped IPv6 unwrapped, zone dropped.
    None when it is not an address at all."""
    text = str(value or "").strip()
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _network(value):
    """`value` (an address or a CIDR) as an ip_network, None if it is neither.
    Host bits are allowed, as nft allows them."""
    text = str(value or "").strip()
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None


def preflight_ssh_connection(value):
    """Parse an `SSH_CONNECTION` value ("client port server port").

    Returns {address, port}: the client address normalized (an IPv4-mapped
    IPv6 address such as ::ffff:192.0.2.10 becomes 192.0.2.10, which is how
    the firewall's `ip saddr` rules see it) and the client port as an int.
    Both are '' / 0 when the value cannot be parsed.
    """
    fields = str(value or "").split()
    address = _address(fields[0]) if len(fields) >= 1 else None
    try:
        port = int(fields[1]) if len(fields) >= 2 else 0
    except ValueError:
        port = 0
    return {"address": str(address) if address is not None else "", "port": port}


def preflight_ssh_unparsed(entries):
    """The entries of an accept list that are neither an address nor a CIDR.

    The firewall still renders them (nft resolves a name at load time, before
    the network is up at boot), but this check cannot count them as covering
    anything.
    """
    return [str(e) for e in (entries or []) if _network(e) is None]


def preflight_ssh_covered(source, allow_from, also=()):
    """Does a host whose `firewall_allow_ssh_from` is `allow_from` accept ssh
    from `source`?

    An empty `allow_from` is the default, ssh from anywhere: covered.
    Otherwise `source` must fall inside one of `allow_from`, one of `also`
    (every other address the host accepts ssh from: its
    firewall_extra_allowed_ipv4 / _ipv6 and the peers roles/firewall adds for
    it -- the controller on nodes and the registry, every node on a backup
    server) or loopback. A `source` that is not an address is never covered.
    """
    if not list(allow_from or []):
        return True
    address = _address(source)
    if address is None:
        return False
    for entry in list(allow_from) + list(also or []) + list(LOOPBACK):
        network = _network(entry)
        if network is not None and network.version == address.version and address in network:
            return True
    return False


class FilterModule:
    def filters(self):
        return {
            "preflight_ssh_connection": preflight_ssh_connection,
            "preflight_ssh_covered": preflight_ssh_covered,
            "preflight_ssh_unparsed": preflight_ssh_unparsed,
        }
