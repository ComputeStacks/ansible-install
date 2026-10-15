"""Pure logic for roles/tailscale (and node_upgrade's maintain preflight,
which includes this role's `tasks_from: status`).

Everything in here is a function of its arguments: the task files feed these
filters what a node printed (`tailscale status --json`, `tailscale debug
prefs`) and act on the answer; tests/tailscale_membership.yml feeds them
canned copies of the same output.

Two questions:

* Is the node logged in to the tailnet? (`tailscale_backend_state`) A logged-
  in node is never joined again, so no run after the first join needs an
  auth key.
* Would the flags this role applies leave a logged-in node's settings as they
  are? (`tailscale_prefs_problems`, `tailscale_set_args`) The role never runs
  `tailscale up` on a logged-in node, so on a difference it stops and says
  what differs and how to reconcile it instead.
"""

import json
import shlex

# BackendState values (ipn.State) this role knows how to act on.
#   Running        logged in and up: never joined again.
#   NeedsLogin     installed, not logged in: the first join.
#   NoState        tailscaled has no state yet: the first join.
#   NotInstalled   (ours) no tailscale binary: install, then the first join.
# Anything else -- Stopped (logged in, `tailscale down`), NeedsMachineAuth
# (waiting for approval), Starting, InUseOtherUser, or an unreadable status --
# is refused by tasks/status.yml with a message naming the state.
JOIN_STATES = ("NeedsLogin", "NoState", "NotInstalled")
LOGGED_IN_STATE = "Running"


def backend_state(status_stdout):
    """BackendState out of `tailscale status --json`, or "" when the output
    is not a JSON document carrying one."""
    try:
        doc = json.loads(status_stdout or "")
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    return str(doc.get("BackendState") or "")


# ---------------------------------------------------------------------------
# Would the role's flags leave a logged-in node as it is?
# ---------------------------------------------------------------------------

# Flag -> (Prefs key, default). The defaults are what `tailscale up` with no
# flag means on a Linux server; `tailscale up` refuses ("requires mentioning
# all non-default flags") or silently changes a pref that differs from them
# when the flag is left out.
_TS_BOOL_FLAGS = {
    "accept-dns": ("CorpDNS", True),
    "accept-routes": ("RouteAll", False),
    "ssh": ("RunSSH", False),
    "shields-up": ("ShieldsUp", False),
    "exit-node-allow-lan-access": ("ExitNodeAllowLANAccess", False),
}
_TS_STRING_FLAGS = {
    "hostname": ("Hostname", ""),
    "operator": ("OperatorUser", ""),
}
_TS_DEFAULT_CONTROL = ("", "https://controlplane.tailscale.com",
                       "https://login.tailscale.com")
_TS_NETFILTER = {"off": 0, "nodivert": 1, "on": 2}
# Not prefs: accepted and ignored.
_TS_IGNORED_FLAGS = ("timeout", "authkey", "auth-key", "qr", "json", "reset",
                     "force-reauth")
# Prefs no `up` flag of this role sets; a non-default value is a node set up
# by hand. Each with the `tailscale set` flag that resets it, if there is one.
_TS_MUST_BE_DEFAULT = (
    ("RunWebClient", False, "--webclient=false"),
    ("AdvertiseServices", None, None),
    ("PostureChecking", False, "--report-posture=false"),
)


def _ts_bool(value):
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _ts_list(value):
    return sorted(v.strip() for v in str(value or "").split(",") if v.strip())


def _ts_parse_args(args):
    flags = {}
    tokens = shlex.split(args or "")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok.startswith("-"):
            flags.setdefault("_positional", []).append(tok)
            continue
        name, sep, value = tok.lstrip("-").partition("=")
        if not sep:
            if name in _TS_BOOL_FLAGS or name in ("advertise-exit-node", "snat-subnet-routes") \
                    or name in _TS_IGNORED_FLAGS:
                value = "true"
            elif i < len(tokens) and not tokens[i].startswith("-"):
                value = tokens[i]
                i += 1
            else:
                value = ""
        flags[name] = value
    return flags


def _compare(prefs_stdout, up_args, tags):
    """[(problem message, `tailscale set` argument or None)]; empty when the
    node and the flags agree. A None argument is a difference `tailscale set`
    cannot reconcile."""
    try:
        prefs = json.loads(prefs_stdout or "")
    except ValueError:
        prefs = None
    if not isinstance(prefs, dict):
        return [("`tailscale debug prefs` printed no JSON document, so the node's "
                 "tailscale settings cannot be compared", None)]
    out = []
    if prefs.get("LoggedOut") or not prefs.get("WantRunning", False):
        out.append(("tailscale on this node is not logged in and running "
                    "(LoggedOut/WantRunning); a node is joined only when it is not "
                    "logged in, with a short-lived tailscale_authkey", None))
    flags = _ts_parse_args(up_args)
    for pos in flags.pop("_positional", []):
        out.append(("unexpected argument %r in the tailscale up flags" % pos, None))

    for flag, (key, default) in _TS_BOOL_FLAGS.items():
        want = _ts_bool(flags.pop(flag)) if flag in flags else default
        have = bool(prefs.get(key, default))
        if have != want:
            out.append(("%s is %s on the node but the role's flags mean --%s=%s"
                        % (key, str(have).lower(), flag, str(want).lower()),
                        "--%s=%s" % (flag, str(want).lower())))
    for flag, (key, default) in _TS_STRING_FLAGS.items():
        want = flags.pop(flag) if flag in flags else default
        have = prefs.get(key) or ""
        if str(have) != str(want):
            out.append(("%s differs between the node and --%s" % (key, flag),
                        "--%s=%s" % (flag, shlex.quote(str(want)))))

    control = flags.pop("login-server", None)
    have_control = prefs.get("ControlURL") or ""
    if control is None:
        if have_control not in _TS_DEFAULT_CONTROL:
            out.append(("ControlURL is not tailscale's default, and the flags have no --login-server", None))
    elif have_control.rstrip("/") != control.rstrip("/"):
        out.append(("ControlURL differs between the node and --login-server", None))

    routes = _ts_list(flags.pop("advertise-routes", ""))
    exit_node = _ts_bool(flags.pop("advertise-exit-node", "false"))
    want_routes = sorted(set(routes) | ({"0.0.0.0/0", "::/0"} if exit_node else set()))
    have_routes = sorted(prefs.get("AdvertiseRoutes") or [])
    if have_routes != want_routes:
        have_exit = {"0.0.0.0/0", "::/0"} <= set(have_routes)
        fix = []
        if sorted(r for r in have_routes if r not in ("0.0.0.0/0", "::/0")) != routes:
            fix.append("--advertise-routes=%s" % ",".join(routes))
        if have_exit != exit_node:
            fix.append("--advertise-exit-node=%s" % str(exit_node).lower())
        out.append(("AdvertiseRoutes on the node (%s) differ from the flags (%s)"
                    % (", ".join(have_routes) or "none", ", ".join(want_routes) or "none"),
                    " ".join(fix) or None))

    snat = _ts_bool(flags.pop("snat-subnet-routes", "true"))
    if bool(prefs.get("NoSNAT", False)) != (not snat):
        out.append(("NoSNAT on the node differs from --snat-subnet-routes",
                    "--snat-subnet-routes=%s" % str(snat).lower()))

    mode = flags.pop("netfilter-mode", "on")
    want_mode = _TS_NETFILTER.get(str(mode).lower())
    if want_mode is None:
        out.append(("--netfilter-mode=%s is not off, nodivert or on" % mode, None))
    elif prefs.get("NetfilterMode", 2) != want_mode:
        out.append(("NetfilterMode on the node differs from --netfilter-mode=%s" % mode,
                    "--netfilter-mode=%s" % str(mode).lower()))

    want_tags = _ts_list(flags.pop("advertise-tags", ""))
    want_tags = sorted(set(want_tags) | {("tag:" + t if not str(t).startswith("tag:") else str(t))
                                         for t in (tags or [])})
    have_tags = sorted(prefs.get("AdvertiseTags") or [])
    if have_tags != want_tags:
        # `tailscale set` cannot change tags; they change only with a re-auth.
        out.append(("AdvertiseTags on the node (%s) differ from the flags and tailscale_tags (%s)"
                    % (", ".join(have_tags) or "none", ", ".join(want_tags) or "none"), None))

    if "exit-node" in flags:
        flags.pop("exit-node")
        out.append(("--exit-node is set; this check cannot verify which exit node the node uses", None))
    elif prefs.get("ExitNodeID") or prefs.get("ExitNodeIP"):
        out.append(("the node uses an exit node, and the flags have no --exit-node", "--exit-node="))

    for key, default, reset in _TS_MUST_BE_DEFAULT:
        value = prefs.get(key)
        if default is None:
            if value:
                out.append(("%s is set on the node; no flag of the role sets it" % key, reset))
        elif value is not None and bool(value) != default:
            out.append(("%s is %s on the node; no flag of the role sets it"
                        % (key, str(bool(value)).lower()), reset))

    for name in _TS_IGNORED_FLAGS:
        flags.pop(name, None)
    for name in sorted(flags):
        out.append(("--%s is not a flag this check can verify; compare it with "
                    "`tailscale debug prefs` by hand" % name, None))
    return out


def tailscale_prefs_problems(prefs_stdout, up_args, tags=None):
    """Compare `tailscale debug prefs` with the flags roles/tailscale applies;
    return a list of problems (empty = they agree).

    The role never runs `tailscale up` on a logged-in node, so a difference
    is never reconciled by re-joining: the run stops before changing
    anything and names it. Messages name the pref and the flag, never a value
    from the prefs beyond the booleans and lists compared.

    Covered: --accept-dns, --accept-routes, --ssh, --shields-up,
    --exit-node-allow-lan-access, --hostname, --operator, --login-server,
    --advertise-routes, --advertise-exit-node, --snat-subnet-routes,
    --netfilter-mode, --advertise-tags (plus the collection's
    `tailscale_tags`), and that no exit node is in use. Any other flag is a
    problem: this check cannot vouch for it.
    """
    return [msg for msg, _ in _compare(prefs_stdout, up_args, tags)]


def tailscale_set_args(prefs_stdout, up_args, tags=None):
    """The `tailscale set` arguments that make the node match the flags, and
    the differences no `tailscale set` flag reconciles:
    {"set": [...], "manual": [...]}."""
    found = _compare(prefs_stdout, up_args, tags)
    return {
        "set": [arg for _, arg in found if arg],
        "manual": [msg for msg, arg in found if not arg],
    }


class FilterModule(object):
    def filters(self):
        return {
            "tailscale_backend_state": backend_state,
            "tailscale_prefs_problems": tailscale_prefs_problems,
            "tailscale_set_args": tailscale_set_args,
        }
