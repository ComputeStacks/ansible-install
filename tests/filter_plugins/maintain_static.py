"""Static checks tests/maintain_nodes.yml runs over the repository's own YAML.

Test-only (a playbook-adjacent filter_plugins/ directory, loaded for the
playbooks in tests/ alone). Each filter takes parsed YAML and returns the
offending items, so the assert that calls it can print them.
"""

DOCKER_UNITS = ("docker", "docker.service")
SERVICE_MODULES = (
    "ansible.builtin.systemd_service", "ansible.builtin.systemd",
    "ansible.builtin.service", "systemd_service", "systemd", "service",
)
COMMAND_MODULES = (
    "ansible.builtin.command", "ansible.builtin.shell", "command", "shell",
)
DEFER_VAR = "docker_tls_restart_deferred"


def _when_text(item):
    when = item.get("when")
    if when is None:
        return ""
    if isinstance(when, list):
        return " and ".join(str(w) for w in when)
    return str(when)


def _restarts_docker(handler):
    for module in SERVICE_MODULES:
        args = handler.get(module)
        if isinstance(args, dict) and str(args.get("name", "")) in DOCKER_UNITS \
                and str(args.get("state", "")) in ("restarted", "stopped"):
            return True
    for module in COMMAND_MODULES:
        args = handler.get(module)
        text = args if isinstance(args, str) else " ".join(
            str(v) for v in (args or {}).values()) if isinstance(args, dict) else ""
        if "docker" in text and ("restart" in text or "stop" in text):
            return True
    return False


def ungated_docker_restarts(handler_files):
    """{role: [handlers]} -> ["role: handler name"] for every handler that
    restarts or stops dockerd without `docker_tls_restart_deferred` in its
    `when` (R1: none may run under live tenants in maintain-nodes)."""
    out = []
    for role, handlers in sorted((handler_files or {}).items()):
        for h in handlers or []:
            if isinstance(h, dict) and _restarts_docker(h) and DEFER_VAR not in _when_text(h):
                out.append("%s: %s" % (role, h.get("name", "<unnamed>")))
    return out


def included_roles(tasks):
    """A task list -> [{name, vars, when}] for every include_role/import_role."""
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        for key in ("ansible.builtin.include_role", "ansible.builtin.import_role",
                    "include_role", "import_role"):
            if key in t:
                out.append({"name": (t[key] or {}).get("name"),
                            "tasks_from": (t[key] or {}).get("tasks_from"),
                            "vars": t.get("vars") or {},
                            "when": _when_text(t)})
        for nested in ("block", "rescue", "always"):
            out.extend(included_roles(t.get(nested)))
    return out


def tasks_with_notify(task_files):
    """{file: tasks} -> ["file: task name"] for every task carrying `notify`."""
    out = []

    def walk(fname, tasks):
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            if "notify" in t:
                out.append("%s: %s" % (fname, t.get("name", "<unnamed>")))
            for nested in ("block", "rescue", "always"):
                walk(fname, t.get(nested))

    for fname, tasks in sorted((task_files or {}).items()):
        walk(fname, tasks)
    return out


def flattened_task_names(tasks):
    """Task names, depth first through blocks (a nameless meta task as
    `meta:<action>`)."""
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        meta = t.get("ansible.builtin.meta", t.get("meta"))
        out.append(t.get("name") or ("meta:%s" % meta if meta else ""))
        for nested in ("block",):
            out.extend(flattened_task_names(t.get(nested)))
    return out


def in_order(names, wanted):
    """True when every name in `wanted` occurs in `names`, in that order."""
    pos = -1
    for w in wanted or []:
        try:
            pos = list(names).index(w, pos + 1)
        except ValueError:
            return False
    return True


class FilterModule(object):
    def filters(self):
        return {
            "maintain_ungated_docker_restarts": ungated_docker_restarts,
            "maintain_included_roles": included_roles,
            "maintain_tasks_with_notify": tasks_with_notify,
            "maintain_flattened_task_names": flattened_task_names,
            "maintain_in_order": in_order,
        }
