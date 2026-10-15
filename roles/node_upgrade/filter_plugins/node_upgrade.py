"""Pure decision logic for roles/node_upgrade.

Everything in here is a function of its arguments: no command runs, nothing
is read off a host. The task files feed these filters what a node printed
(`docker container inspect`, `docker ps --format json`, `dpkg-query`,
`cs-agent maintenance ... --json`) and act on the answer, and
tests/node_upgrade_select.yml feeds them canned copies of the same output to
prove the rules without a node.

They live in a filter plugin rather than in Jinja set_fact chains because two
of them -- the container selection and the `maintenance on` classification --
are the rules the whole upgrade's safety rests on, and a rule spelled once in
Python with a docstring is easier to get right, and to keep right, than the
same rule spread over a dozen `selectattr` lines.
"""

import json
import math
import re

# The label every ComputeStacks container carries; `system` marks the
# node-local services that a systemd unit runs as `docker run --rm` (cadvisor,
# fluentd). See select_running_tenants() for why those are never recorded.
ROLE_LABEL = "com.computestacks.role"
SYSTEM_ROLE = "system"


def _labels(container):
    config = container.get("Config") or {}
    return config.get("Labels") or {}


def _is_system(container):
    return _labels(container).get(ROLE_LABEL) == SYSTEM_ROLE


def _name(container):
    # inspect reports the name with docker's leading slash; `docker ps` and
    # every operator-facing message use it without one.
    return (container.get("Name") or "").lstrip("/")


def parse_inspect(stdout):
    """`docker container inspect` output (a JSON array) -> a list.

    Empty output (no containers at all) is an empty list, not an error.
    """
    text = (stdout or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("docker container inspect did not print a JSON array")
    return data


def select_running_tenants(containers):
    """The containers this upgrade must stop and later start again.

    The rule: State.Running is true AND label com.computestacks.role is not
    `system`. Returned as [{id, name}] in input order.

    * `system` containers are systemd units running `docker run --rm`: their
      unit brings them back after docker restarts, and docker deletes them on
      stop, so a recorded id would never exist again to be started.
    * Everything else is created by the controller and has NO docker restart
      policy -- tenant services, SFTP bastions (role=bastion), registry
      containers -- so nothing but this playbook brings it back before the
      hold is released.
    * A container that is NOT running is left alone and stays that way. That
      is the whole point of recording: the manual procedure this replaces
      (`docker start $(docker ps -aq)`) also started containers that had been
      stopped on purpose.
    """
    out = []
    for c in containers or []:
        state = c.get("State") or {}
        if state.get("Running") is True and not _is_system(c):
            out.append({"id": c.get("Id"), "name": _name(c)})
    return out


def has_tenant_containers(containers):
    """True when ANY container, in any state, is not a `system` one.

    This is what "live" means for the site.yml guards (tasks/detect_live.yml):
    a node the controller has placed something on. A stopped tenant container
    still counts -- the controller may start it at any moment, and a node
    whose tenants all happen to be stopped is still a production node.
    """
    return any(not _is_system(c) for c in containers or [])


def parse_ps(lines):
    """`docker ps -a --no-trunc --format json` stdout_lines -> [{id, name, running}].

    One JSON object per line. `Names` is a comma-separated list in the CLI's
    format but a container has exactly one name unless legacy links are in
    use, so the first entry is taken.
    """
    out = []
    for line in lines or []:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        names = (row.get("Names") or "").split(",")
        out.append({
            "id": row.get("ID"),
            "name": names[0].lstrip("/") if names else "",
            "running": row.get("State") == "running",
        })
    return out


def resolve_recorded(recorded, ps):
    """Match every recorded {id, name} against the current container list.

    Matching is by id, falling back to name: ids survive a docker restart and
    a reboot, so the fallback only fires when a container was recreated under
    the same name (which the controller should not do while the node is held,
    but an operator might). Returns, per recorded entry:

        {id, name, current_id, running, found}

    `current_id` is None (and `found` False) when neither the id nor the name
    exists any more -- a container removed during the window.
    """
    by_id = {c["id"]: c for c in ps or []}
    by_name = {c["name"]: c for c in ps or []}
    out = []
    for r in recorded or []:
        cur = by_id.get(r.get("id")) or by_name.get(r.get("name"))
        out.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "current_id": cur["id"] if cur else None,
            "running": bool(cur and cur["running"]),
            "found": cur is not None,
        })
    return out


def accept_down_list(spec):
    """`-e node_upgrade_accept_down=a,b` (or a YAML list) -> a clean list."""
    if spec is None:
        return []
    if isinstance(spec, str):
        items = spec.split(",")
    else:
        items = list(spec)
    return [str(i).strip() for i in items if str(i).strip()]


def is_accepted_down(entry, accepted):
    """Whether a resolved/recorded entry is named in the accept-down list.

    By id (full or a prefix of at least 12 characters, the short form `docker
    ps` prints) or by name.
    """
    for a in accepted or []:
        if a == entry.get("name"):
            return True
        for cid in (entry.get("id"), entry.get("current_id")):
            if cid and len(a) >= 12 and cid.startswith(a):
                return True
    return False


def partition_accepted(entries, accepted):
    """Split recorded/resolved entries into {required, accepted}.

    `accepted` are the entries named in node_upgrade_accept_down: they are
    still started, but one that stays down does not fail the run.
    """
    out = {"required": [], "accepted": []}
    for e in entries or []:
        key = "accepted" if is_accepted_down(e, accepted) else "required"
        out[key].append(e)
    return out


def parse_dpkg_status(lines):
    """`dpkg-query -W -f '${Package} ${db:Status-Status} ${Version}\\n' ...`
    stdout_lines -> {package: version} for INSTALLED packages only.

    A package that is absent or only has config files left is not in the
    map; dpkg-query reports it on stderr, or with a status other than
    `installed`.
    """
    out = {}
    for line in lines or []:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "installed":
            out[parts[0]] = parts[2]
    return out


def split_pin(entry):
    """`name=version` -> (name, version); a bare name -> (name, None)."""
    name, sep, version = str(entry).partition("=")
    return name, (version if sep else None)


def docker_drift(installed, pins):
    """The pinned packages whose installed state differs from the pin.

    Returns [{name, pin, installed}] -- `installed` is None for a package that
    is not installed at all, which counts as drift: the pinned set changed,
    and making the node match it means an apt transaction on docker's
    packages. An unpinned entry (bare name) drifts only when it is absent.
    """
    out = []
    for entry in pins or []:
        name, pin = split_pin(entry)
        have = (installed or {}).get(name)
        if have is None or (pin is not None and have != pin):
            out.append({"name": name, "pin": pin, "installed": have})
    return out


def cli_json(stdout):
    """One `cs-agent maintenance ... --json` document -> {shape, doc}.

    shape is `status` (the status object), `error` (`{"error", "exit_code"}`)
    or `none` (empty or unparseable stdout -- e.g. the binary is missing, or
    a cs-agent too old to know `--json`). `--json` prints exactly one object
    per exit, so the first non-empty line is the document.
    """
    text = ""
    for line in (stdout or "").splitlines():
        if line.strip():
            text = line.strip()
            break
    try:
        doc = json.loads(text) if text else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return {"shape": "none", "doc": {}}
    if "error" in doc:
        return {"shape": "error", "doc": doc}
    if "paused" in doc and "local" in doc:
        return {"shape": "status", "doc": doc}
    return {"shape": "none", "doc": doc}


def classify_on(stdout, rc):
    """Classify a `cs-agent maintenance on --wait --json` exit.

    JSON SHAPE FIRST, rc second -- verified against cs-agent's
    maintenance_cli.go (maintCLI.on / wait / fail / failWith / failNoStatus),
    where the same rc is produced by paths that leave the node in different
    states, and only the document tells them apart:

    error shape {"error", "exit_code"} -- failNoStatus: no status could be
      read, so whether a hold was placed is UNKNOWN. It is produced with
        1  usage errors (before any hold), or `interrupted` when the final
           status read failed (hold placed);
        2  timed out AND the state was unreadable (hold placed);
        3  control.db unavailable (before or after the hold write);
        4  the up-front "controller has not polled with maintenance support"
           check when the status read failed (not placed), or the deadline
           case when it failed (placed).
      held: None.

    status shape -- failWith/emit: the document is the node's maintenance
      status AFTER the call, so `local` says whether a local hold exists.
      The caller only runs `on` when no local hold existed beforehand
      (tasks/hold.yml), so a `local` here is the hold this call placed.
        0  quiesced, controller acknowledged, settle elapsed     -> ok
        2  timed out with work still in flight; `running` lists it
           (quiesce `busy`, or `unknown` when docker could not list the
           backup containers)                                     -> timeout
        4  `local` null: the up-front controller check failed and NO hold
           was placed                                             -> not_placed
           `local` set: quiesced but the controller never confirmed the
           hold by the deadline                                  -> unconfirmed
        1  `local` null: "the local hold was cleared while waiting" (someone
           ran `off`)                                            -> cleared
           `local` set: "interrupted; the local hold stays in place" (the
           CLI got SIGINT/SIGTERM)                                -> interrupted
        any other rc with a status document                     -> failed
      held: whether `local` is set.

    no JSON at all -> outcome `no_output`, held None.

    Returns {outcome, ok, rc, shape, held, local, controller, running,
    quiesce, controller_acked, error}.
    """
    rc = int(rc) if rc is not None else None
    parsed = cli_json(stdout)
    doc = parsed["doc"]
    res = {
        "rc": rc,
        "shape": parsed["shape"],
        "ok": False,
        "held": None,
        "local": None,
        "controller": None,
        "running": [],
        "quiesce": None,
        "controller_acked": None,
        "error": None,
    }

    if parsed["shape"] == "error":
        res["outcome"] = "error"
        res["error"] = doc.get("error")
        return res

    if parsed["shape"] != "status":
        res["outcome"] = "no_output"
        return res

    local = doc.get("local")
    res.update({
        "held": local is not None,
        "local": local,
        "controller": doc.get("controller"),
        "running": doc.get("running") or [],
        "quiesce": doc.get("quiesce"),
        "controller_acked": doc.get("controller_acked"),
    })
    if rc == 0:
        # rc 0 is only ever emitted with the local hold in place (wait
        # re-checks it on every sample). A status document without one is a
        # CLI this role does not understand -- never treat it as success.
        if local is not None:
            res["outcome"] = "ok"
            res["ok"] = True
        else:
            res["outcome"] = "failed"
    elif rc == 2:
        res["outcome"] = "timeout"
    elif rc == 4:
        res["outcome"] = "unconfirmed" if local is not None else "not_placed"
    elif rc == 1:
        res["outcome"] = "interrupted" if local is not None else "cleared"
    else:
        res["outcome"] = "failed"
    return res


# ---------------------------------------------------------------------------
# playbooks/maintain-nodes.yml
# ---------------------------------------------------------------------------

# Which playbook owns a state.json. A file written before the field existed
# can only have come from upgrade-docker.yml, the one playbook that wrote
# state files then.
DEFAULT_STATE_OWNER = "upgrade-docker"


def state_owner(state):
    """The playbook a state.json belongs to (`playbook`, else upgrade-docker).

    Each playbook refuses to resume the other's state (tasks/state_load.yml):
    their phases differ, and resuming another playbook's half-finished cycle
    would skip or repeat steps that playbook alone knows the order of.
    """
    if not isinstance(state, dict):
        return DEFAULT_STATE_OWNER
    owner = state.get("playbook")
    if not owner:
        return DEFAULT_STATE_OWNER
    return str(owner)


def agent_status(stdout, rc=None):
    """Read one `cs-agent maintenance status --json` while waiting for the
    agent to come back after its package install / restart.

    Returns {done, ready, state, held, agent_active, rc, error}:

    * status document, `local` null      -> done, NOT ready, state `unheld`:
      the hold this run placed is gone. Never retried: a restarted agent
      comes up with the hold it had (control.db), so a missing one was
      removed by somebody.
    * status document, `local` set, `agent_active` true -> done, ready.
    * status document, `local` set, `agent_active` false or missing -> not
      done (state `agent_inactive`): the service is still restarting.
    * error document with exit_code / rc 3 (control.db unavailable, e.g. the
      agent holds its lock while starting) -> not done, state `unavailable`.
    * error document with any other code (usage: a CLI that does not
      understand the call) -> done, NOT ready, state `error`.
    * nothing parseable (the binary is being replaced by dpkg) -> not done,
      state `no_output`.

    `controller_acked` is deliberately NOT read: after an agent restart the
    quiesce samples bump `seq`, so it can read false for a moment while the
    node is still held -- that is not "unheld". Extra keys, `quiesce:
    "unknown"` and a null `sample` are all tolerated: only `paused`, `local`
    and `agent_active` are looked at.
    """
    rc = int(rc) if rc is not None else None
    parsed = cli_json(stdout)
    doc = parsed["doc"]
    res = {"done": False, "ready": False, "state": "no_output", "held": None,
           "agent_active": None, "rc": rc, "error": None}
    if parsed["shape"] == "error":
        code = doc.get("exit_code", rc)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = rc
        res["error"] = doc.get("error")
        if code == 3:
            res["state"] = "unavailable"
        else:
            res.update({"done": True, "state": "error"})
        return res
    if parsed["shape"] != "status":
        return res
    local = doc.get("local")
    active = doc.get("agent_active") is True
    res.update({"held": local is not None, "agent_active": active})
    if local is None:
        res.update({"done": True, "state": "unheld"})
    elif active:
        res.update({"done": True, "ready": True, "state": "ready"})
    else:
        res["state"] = "agent_inactive"
    return res


def maintain_decide(inputs):
    """Whether a maintain-nodes run must stop the node's tenant containers.

    inputs: {docker_drift: [{name, pin, installed}],
             docker_restart_reasons: [str],
             reboot_pending: bool, force: bool}

    Stop iff ANY of:
      * docker drifts from its pins (the package install restarts dockerd);
      * dockerd must restart to load what the converge changed (docker_tls'
        certificate or ExecStart drop-in; the restart was deferred);
      * /var/run/reboot-required already exists;
      * maintain_force_restart.

    Pending OS updates are NOT an input: they are applied only inside a
    stopped window that happens for one of the reasons above (the same rule
    as upgrade-docker), and never with tenants running.

    Returns {stop, reasons, docker_restart_required, docker_restart_reasons,
    drift, reboot_pending, force} -- everything the state file records with
    the `converged` phase, so a resumed run decides from it.
    """
    inputs = inputs or {}
    drift = list(inputs.get("docker_drift") or [])
    restart = []
    for r in inputs.get("docker_restart_reasons") or []:
        r = str(r).strip()
        if r and r not in restart:
            restart.append(r)
    reboot = bool(inputs.get("reboot_pending"))
    force = bool(inputs.get("force"))
    reasons = []
    if drift:
        reasons.append("docker drifts from its pins (" + "; ".join(
            "%s %s -> %s" % (d.get("name"), d.get("installed") or "absent",
                             d.get("pin") or "any")
            for d in drift) + ")")
    for r in restart:
        reasons.append("docker must restart: " + r)
    if reboot:
        reasons.append("a reboot is already pending (/var/run/reboot-required)")
    if force:
        reasons.append("forced (maintain_force_restart)")
    return {
        "stop": bool(reasons),
        "reasons": reasons,
        "docker_restart_required": bool(restart),
        "docker_restart_reasons": restart,
        "drift": drift,
        "reboot_pending": reboot,
        "force": force,
    }


# Packages whose upgrade makes Ubuntu ask for a reboot: the kernel (a new
# linux-image / linux-modules) and the C library every running process maps.
REBOOT_PACKAGE_PATTERNS = (
    re.compile(r"^linux-(image|modules|modules-extra|generic|virtual|kvm|headers)(-|$)"),
    re.compile(r"^libc6(:|$)"),
    re.compile(r"^libc-bin(:|$)"),
)


def parse_apt_sim(stdout):
    """`apt-get -s upgrade --with-new-pkgs` stdout -> the pending OS updates.

    Returns {count, packages, reboot_packages, reboot_hint}. Only `Inst`
    lines count (the `Conf` lines repeat them). Held packages -- docker's --
    are never listed by apt as installs, so they are not here.
    """
    pkgs = []
    for line in (stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Inst" and parts[1] not in pkgs:
            pkgs.append(parts[1])
    reboot = [p for p in pkgs if any(rx.match(p) for rx in REBOOT_PACKAGE_PATTERNS)]
    return {"count": len(pkgs), "packages": pkgs,
            "reboot_packages": reboot, "reboot_hint": bool(reboot)}


def systemd_unix_ts(stdout):
    """`systemctl show <unit> -p ActiveEnterTimestamp --timestamp=unix`
    stdout (`ActiveEnterTimestamp=@1791400000`) -> 1791400000, or 0 when the
    unit never entered `active` (empty value or `n/a`)."""
    for line in (stdout or "").splitlines():
        _, _, value = line.partition("=")
        value = value.strip().lstrip("@")
        if value.isdigit():
            return int(value)
    return 0


def files_newer_than(stats, epoch):
    """The paths in `stats` modified after `epoch`.

    `stats` is [{path, exists, mtime}], or the `results` of a looped
    ansible.builtin.stat ([{item: <path>, stat: {exists, mtime}}]).

    Used to tell whether dockerd is still running with docker_tls files
    older than what is on disk: a certificate or drop-in written after
    dockerd last started is not loaded yet, whatever the run that wrote it
    recorded. That covers a run that died between writing them and the
    `converged` phase, where the deferred handler's note is lost. A missing
    file is not "newer". epoch 0 (dockerd not running) makes every existing
    file newer. Compared in whole seconds (see below).
    """
    try:
        epoch = float(epoch or 0)
    except (TypeError, ValueError):
        epoch = 0.0
    out = []
    for s in stats or []:
        if "stat" in s:
            st = s.get("stat") or {}
            row = {"path": s.get("item"), "exists": st.get("exists"),
                   "mtime": st.get("mtime")}
        else:
            row = s
        # Whole seconds: systemd reports the start truncated to the second, so
        # a file written in the same second as the start (before it) must not
        # count as newer.
        if row.get("exists") and math.floor(float(row.get("mtime") or 0)) > epoch:
            out.append(row.get("path"))
    return out


# ---------------------------------------------------------------------------
# Tailscale: would `tailscale up` with the role's flags leave a node as it is?
# ---------------------------------------------------------------------------

# Flag -> (Prefs key, kind, default). The defaults are what `tailscale up`
# with no flag means on a Linux server; `tailscale up` refuses ("requires
# mentioning all non-default flags") or silently changes a pref that differs
# from them when the flag is left out.
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
# by hand, which `up` would then fight.
_TS_MUST_BE_DEFAULT = (
    ("RunWebClient", False),
    ("AdvertiseServices", None),
    ("PostureChecking", False),
)


def _ts_bool(value):
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _ts_list(value):
    return sorted(v.strip() for v in str(value or "").split(",") if v.strip())


def _ts_parse_args(args):
    import shlex
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


def tailscale_prefs_problems(prefs_stdout, up_args, tags=None):
    """Compare `tailscale debug prefs` with the flags roles/tailscale passes
    to `tailscale up`; return a list of problems (empty = they agree).

    A node joined by hand carries no artis3n state file, so the first
    converge runs `tailscale up` with the role's flags: tailscale either
    refuses ("requires mentioning all non-default flags") -- failing the run
    inside a maintenance hold -- or quietly changes a setting the node has
    been relying on. maintain-nodes therefore compares first, before any
    hold, and refuses on any difference. Messages name the pref and the flag,
    never a value from the prefs beyond the booleans and lists compared.

    Covered: --accept-dns, --accept-routes, --ssh, --shields-up,
    --exit-node-allow-lan-access, --hostname, --operator, --login-server,
    --advertise-routes, --advertise-exit-node, --snat-subnet-routes,
    --netfilter-mode, --advertise-tags (plus the collection's
    `tailscale_tags`), and that no exit node is in use. Any other flag is a
    problem: this check cannot vouch for it.
    """
    try:
        prefs = json.loads(prefs_stdout or "")
    except ValueError:
        prefs = None
    if not isinstance(prefs, dict):
        return ["`tailscale debug prefs` printed no JSON document, so the node's "
                "tailscale settings cannot be compared"]
    problems = []
    if prefs.get("LoggedOut") or not prefs.get("WantRunning", False):
        problems.append("tailscale on this node is not logged in and running "
                        "(LoggedOut/WantRunning); maintain-nodes never joins a node "
                        "to the tailnet -- do that with site.yml/add-region.yml or by hand")
    flags = _ts_parse_args(up_args)
    for pos in flags.pop("_positional", []):
        problems.append("unexpected argument %r in the tailscale up flags" % pos)

    for flag, (key, default) in _TS_BOOL_FLAGS.items():
        want = _ts_bool(flags.pop(flag)) if flag in flags else default
        have = bool(prefs.get(key, default))
        if have != want:
            problems.append("%s is %s on the node but the role's flags mean --%s=%s"
                            % (key, str(have).lower(), flag, str(want).lower()))
    for flag, (key, default) in _TS_STRING_FLAGS.items():
        want = flags.pop(flag) if flag in flags else default
        have = prefs.get(key) or ""
        if str(have) != str(want):
            problems.append("%s differs between the node and --%s" % (key, flag))

    control = flags.pop("login-server", None)
    have_control = prefs.get("ControlURL") or ""
    if control is None:
        if have_control not in _TS_DEFAULT_CONTROL:
            problems.append("ControlURL is not tailscale's default, and the flags have no --login-server")
    elif have_control.rstrip("/") != control.rstrip("/"):
        problems.append("ControlURL differs between the node and --login-server")

    routes = _ts_list(flags.pop("advertise-routes", ""))
    if _ts_bool(flags.pop("advertise-exit-node", "false")):
        routes = sorted(set(routes) | {"0.0.0.0/0", "::/0"})
    have_routes = sorted(prefs.get("AdvertiseRoutes") or [])
    if have_routes != routes:
        problems.append("AdvertiseRoutes on the node (%s) differ from the flags (%s)"
                        % (", ".join(have_routes) or "none", ", ".join(routes) or "none"))

    snat = _ts_bool(flags.pop("snat-subnet-routes", "true"))
    if bool(prefs.get("NoSNAT", False)) != (not snat):
        problems.append("NoSNAT on the node differs from --snat-subnet-routes")

    mode = flags.pop("netfilter-mode", "on")
    want_mode = _TS_NETFILTER.get(str(mode).lower())
    if want_mode is None:
        problems.append("--netfilter-mode=%s is not off, nodivert or on" % mode)
    elif prefs.get("NetfilterMode", 2) != want_mode:
        problems.append("NetfilterMode on the node differs from --netfilter-mode=%s" % mode)

    want_tags = _ts_list(flags.pop("advertise-tags", ""))
    want_tags = sorted(set(want_tags) | {("tag:" + t if not str(t).startswith("tag:") else str(t))
                                         for t in (tags or [])})
    have_tags = sorted(prefs.get("AdvertiseTags") or [])
    if have_tags != want_tags:
        problems.append("AdvertiseTags on the node (%s) differ from the flags and tailscale_tags (%s)"
                        % (", ".join(have_tags) or "none", ", ".join(want_tags) or "none"))

    if "exit-node" in flags:
        flags.pop("exit-node")
        problems.append("--exit-node is set; this check cannot verify which exit node the node uses")
    elif prefs.get("ExitNodeID") or prefs.get("ExitNodeIP"):
        problems.append("the node uses an exit node, and the flags have no --exit-node")

    for key, default in _TS_MUST_BE_DEFAULT:
        value = prefs.get(key)
        if default is None:
            if value:
                problems.append("%s is set on the node; no flag of the role sets it" % key)
        elif value is not None and bool(value) != default:
            problems.append("%s is %s on the node; no flag of the role sets it"
                            % (key, str(bool(value)).lower()))

    for name in _TS_IGNORED_FLAGS:
        flags.pop(name, None)
    for name in sorted(flags):
        problems.append("--%s is not a flag this check can verify; compare it with "
                        "`tailscale debug prefs` by hand" % name)
    return problems


class FilterModule(object):
    def filters(self):
        return {
            "node_upgrade_parse_inspect": parse_inspect,
            "node_upgrade_select": select_running_tenants,
            "node_upgrade_has_tenants": has_tenant_containers,
            "node_upgrade_parse_ps": parse_ps,
            "node_upgrade_resolve": resolve_recorded,
            "node_upgrade_accept_down_list": accept_down_list,
            "node_upgrade_partition_accepted": partition_accepted,
            "node_upgrade_parse_dpkg": parse_dpkg_status,
            "node_upgrade_drift": docker_drift,
            "node_upgrade_cli_json": cli_json,
            "node_upgrade_classify_on": classify_on,
            "node_upgrade_state_owner": state_owner,
            "node_upgrade_agent_status": agent_status,
            "node_upgrade_maintain_decide": maintain_decide,
            "node_upgrade_parse_apt_sim": parse_apt_sim,
            "node_upgrade_systemd_unix_ts": systemd_unix_ts,
            "node_upgrade_files_newer_than": files_newer_than,
            "node_upgrade_tailscale_prefs_problems": tailscale_prefs_problems,
        }
