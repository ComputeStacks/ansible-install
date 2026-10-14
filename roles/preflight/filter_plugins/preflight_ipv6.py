"""Pure address checks for roles/preflight.

`public_ipv6` (an optional node host var) is compared as a STRING everywhere
downstream: against what `pdnsutil list-zone` prints for an AAAA rrset
(roles/powerdns, tasks/lb_records_plan.yml), against what `dig` returns
(roles/validate, roles/preflight) and in the controller seed's plan output
(roles/controller_seed). PowerDNS and dig both print the RFC 5952 form --
lowercase, leading zeros dropped, the longest zero run compressed -- so
preflight requires the inventory to use exactly that form, and every other
role can then compare strings without a filter of its own.

Python's `ipaddress` rather than ansible.utils' `ipaddr`: that collection is
not a dependency of this repository, and it needs `netaddr` on the control
machine besides.
"""

import ipaddress


def cs_ipv6_problem(value):
    """Return '' when `value` is a usable public IPv6 address in canonical
    form, otherwise a sentence saying what is wrong with it.

    Usable: a single host address (no prefix length, no zone index) that is
    not unspecified, loopback, link-local, multicast, site-local or an
    IPv4-mapped address -- none of those can be a load balancer's public
    address. ULA (fc00::/7) and the documentation range (2001:db8::/32) are
    accepted, for private test environments and examples, exactly as a
    private `public_ip` is.
    """
    if value is None:
        return "it is empty"
    text = str(value).strip()
    if text != str(value):
        return "it has leading or trailing whitespace"
    if text == "":
        return "it is empty"
    if "/" in text:
        return "it carries a prefix length; give the single address, without /128 or /64"
    if "%" in text:
        return "it carries a zone index (%...), which only a link-local address has"
    try:
        addr = ipaddress.IPv6Address(text)
    except ValueError:
        return "it is not an IPv6 address"
    if addr.ipv4_mapped is not None:
        return "it is an IPv4-mapped address; put the IPv4 address in public_ip instead"
    if addr.is_unspecified:
        return "it is the unspecified address ::"
    if addr.is_loopback:
        return "it is the loopback address ::1"
    if addr.is_link_local:
        return "it is link-local (fe80::/10), which is never reachable from outside its link"
    if addr.is_multicast:
        return "it is a multicast address"
    if addr.is_site_local:
        return "it is a deprecated site-local address (fec0::/10)"
    canonical = str(addr)
    if text != canonical:
        return (
            "it is not written in canonical (RFC 5952) form; write it as "
            + canonical
            + " -- PowerDNS and dig print that form, and the provisioner compares addresses as text"
        )
    return ""


class FilterModule(object):
    def filters(self):
        return {"cs_ipv6_problem": cs_ipv6_problem}
