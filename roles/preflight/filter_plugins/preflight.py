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


class FilterModule:
    def filters(self):
        return {
            "preflight_unreachable": preflight_unreachable,
        }
