"""Pure decision logic for roles/preflight.

preflight_unreachable() decides which targeted hosts did not answer the
reachability probe in tasks/reachable.yml. It is a function of its arguments:
the hosts the play targeted and a hostvars-like mapping carrying each host's
registered ping result. tests/preflight_reachable.yml feeds it canned results
to prove the rule without a host.
"""

from collections.abc import Mapping

# Long ssh errors (a whole banner, a stack of -v output) are cut to this many
# characters; the full text is in the ping task's own output just above.
REASON_MAX = 240


def _reason(result):
    """Why `result` is not a successful ping, or '' when it is one."""
    if not isinstance(result, Mapping):
        return "no ping result: ansible dropped the host from the play before it ran"
    if result.get("ping") == "pong":
        return ""
    if result.get("unreachable"):
        lead = "unreachable"
    elif result.get("skipped"):
        lead = "ping skipped"
    else:
        lead = "ping failed"
    # A module failure's msg is just "MODULE FAILURE"; the why is in stderr.
    parts = [result.get(k) for k in ("msg", "module_stderr", "module_stdout")]
    detail = " ".join(" ".join(str(p).split()) for p in parts if p)
    if len(detail) > REASON_MAX:
        detail = detail[:REASON_MAX] + "..."
    return f"{lead}: {detail}" if detail else lead


def preflight_unreachable(hosts, hostvars, result_var="preflight_reachable_ping"):
    """Every host in `hosts` whose `result_var` is not a successful ping.

    `hosts` is the play's full target list (ansible_play_hosts_all), so a host
    ansible removed from the play -- and therefore never registered a result --
    is reported too, not silently forgotten. `hostvars` is ansible's hostvars or
    any mapping of host name to a mapping of its variables.

    Returns a list of {host, address, port, reason}, in `hosts` order. `port`
    is the host's ansible_port, or None when it sets none (ssh then uses its
    own default or ~/.ssh/config).
    """
    found = []
    for host in hosts or []:
        try:
            hv = hostvars[host]
        except KeyError:
            hv = {}
        result = hv.get(result_var) if isinstance(hv, Mapping) else None
        reason = _reason(result)
        if not reason:
            continue
        found.append({
            "host": host,
            "address": hv.get("ansible_host") or host,
            "port": hv.get("ansible_port"),
            "reason": reason,
        })
    return found


# Inventory left over from the retired bundled PowerDNS and acme.sh
# nsupdate/pdns support.
LEGACY_GROUPS = ("nameservers", "ns_primary", "ns_followers")
LEGACY_VAR_PREFIXES = ("pdns_", "powerdns_")
LEGACY_VARS = ("dns_driver",)
REMOVED_ACME_PROVIDERS = ("nsupdate", "pdns")


def preflight_legacy_dns(groups, variables):
    """Retired nameserver settings found in the inventory.

    `groups` is ansible's `groups` mapping, `variables` the host's `vars`.
    Returns `groups` (legacy groups that have hosts: fatal),
    `ignored_vars` (legacy variables that are defined: a warning) and
    `acme_removed_provider` (the acme_challenge_method when it names a removed
    acme.sh provider, else ''; fatal).
    """
    found = [g for g in LEGACY_GROUPS if groups.get(g)]
    ignored = sorted(
        k
        for k in variables
        if k in LEGACY_VARS or k.startswith(LEGACY_VAR_PREFIXES)
    )
    method = str(variables.get("acme_challenge_method") or "")
    return {
        "groups": found,
        "ignored_vars": ignored,
        "acme_removed_provider": method if method in REMOVED_ACME_PROVIDERS else "",
    }


class FilterModule:
    def filters(self):
        return {
            "preflight_unreachable": preflight_unreachable,
            "preflight_legacy_dns": preflight_legacy_dns,
        }
