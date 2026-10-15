"""Which agent.yml keys must never change on a running node, and whether a
render would change them.

tasks/immutable_check.yml hands cs_agent_immutable_changes the node's current
/etc/computestacks/agent.yml and what this role would render now;
tests/maintain_nodes.yml feeds it canned documents. Nothing here reads a host.

playbooks/maintain-nodes.yml converges a LIVE node -- inside a cs-agent
maintenance hold, with its tenant containers running -- and refuses, before
it takes the hold, to touch a node whose render would move any of these:

* store.data_dir             where control.db lives: the hold itself, the
                             task queue, the changelog. A new path is an
                             empty database and a node that has forgotten it
                             is in maintenance.
* metadata.admin_token_hash  the controller's admin Bearer. A different hash
                             locks the controller out of the node.
* metadata.listen_addr       the metadata front door customer containers and
                             the controller dial.
* backups.key                the borg passphrase every existing repository on
                             the backup server is encrypted with. A changed
                             value (a re-generated key, or a migrated one that
                             gained a trailing newline) makes every backup of
                             the node unreadable to the agent.
* backups.borg.ssh.{user, host, port, host_path}
                             where those repositories live.

The comparison is by sha256 of each value's canonical JSON, so the result --
and therefore any message built from it -- carries key NAMES only, never a
value: agent.yml holds the passphrase and the token hash.
"""

import hashlib
import json

import yaml

IMMUTABLE_KEYS = (
    "store.data_dir",
    "metadata.admin_token_hash",
    "metadata.listen_addr",
    "backups.key",
    "backups.borg.ssh.user",
    "backups.borg.ssh.host",
    "backups.borg.ssh.port",
    "backups.borg.ssh.host_path",
)

_ABSENT = object()


def _load(doc):
    if isinstance(doc, dict):
        return doc
    if doc is None:
        return {}
    if isinstance(doc, (bytes, bytearray)):
        doc = doc.decode("utf-8")
    loaded = yaml.safe_load(str(doc)) if str(doc).strip() else {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("agent.yml is not a YAML mapping")
    return loaded


def _get(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _ABSENT
        cur = cur[part]
    return cur


def _digest(value):
    if value is _ABSENT:
        return None
    # Scalars compare as strings: an older render that wrote `port: 22` and a
    # newer one that writes `port: "22"` mean the same thing to the agent.
    if not isinstance(value, (dict, list)):
        value = "" if value is None else str(value)
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def immutable_changes(current, rendered, keys=None):
    """[{key, change}] for every immutable key the render would change.

    `change` is `changed`, `added` (absent now, present in the render) or
    `removed`. Empty when the render leaves every one of them as it is.
    `current` and `rendered` are agent.yml texts (or already-parsed dicts).
    """
    cur = _load(current)
    new = _load(rendered)
    out = []
    for key in keys or IMMUTABLE_KEYS:
        a = _digest(_get(cur, key))
        b = _digest(_get(new, key))
        if a == b:
            continue
        if a is None:
            change = "added"
        elif b is None:
            change = "removed"
        else:
            change = "changed"
        out.append({"key": key, "change": change})
    return out


class FilterModule(object):
    def filters(self):
        return {
            "cs_agent_immutable_changes": immutable_changes,
        }
