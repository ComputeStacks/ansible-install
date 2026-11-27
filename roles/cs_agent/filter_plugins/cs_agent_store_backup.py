"""Pure validation of cs-agent's off-node SQLite backup (store.backup.*).

tasks/store_backup_check.yml builds one dict from the role's
`cs_agent_store_backup_*` variables and hands it to
cs_agent_store_backup_problems; tests/cs_agent_store_backup.yml feeds it
canned dicts. Nothing here reads a host.

The rules are cs-agent's own (dbbackup/config.go, main.go
validateStoreBackupS3), checked here because a misconfigured agent only LOGS
"Off-node SQLite backup disabled" at startup and carries on with backups
off, which nobody sees until a node has to be restored:

* nothing is checked while `enabled` is false;
* at least one age X25519 recipient (age1...), each well-formed;
* keep_all_hours >= 0, retention_days >= 1;
* a schedule the agent can parse (5-field cron or an @descriptor; the agent
  silently falls back to its default interval on a bad one);
* the EFFECTIVE S3 settings: every store.backup.s3 key left unset falls back
  to the same key under backups.export.s3, so a bucket and both credentials
  must come from one or the other, and an endpoint must be https.

The dict carries `*_set` / `*_placeholder` booleans for the credentials,
never the keys, so no message can echo one.
"""

import re

_INT_RE = re.compile(r"^-?[0-9]+$")
# bech32: "age1" + data in the bech32 charset (no 1, b, i, o). An X25519
# recipient is 62 characters in all.
_AGE_RE = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
_CRON_FIELD_RE = re.compile(r"^[0-9A-Za-z*/,\-?]+$")
_CRON_DESCRIPTORS = ("@yearly", "@annually", "@monthly", "@weekly",
                     "@daily", "@midnight", "@hourly")


def _as_int(value):
    """An int, or None for anything that is not a whole number (bools too)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_RE.match(value.strip()):
        return int(value.strip())
    return None


def _truthy(value):
    """A bool from a bool or its templated string form ("True", "false")."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _text(value):
    return "" if value is None else str(value).strip()


def _cron_ok(expr):
    expr = _text(expr)
    if expr.startswith("@every "):
        return bool(_text(expr[len("@every "):]))
    if expr in _CRON_DESCRIPTORS:
        return True
    fields = expr.split()
    return len(fields) == 5 and all(_CRON_FIELD_RE.match(f) for f in fields)


def cs_agent_store_backup_problems(cfg):
    """Every problem with a store backup config, as operator-facing sentences.

    An empty list means the config is valid, or that the backup is off.
    """
    cfg = dict(cfg or {})
    if not _truthy(cfg.get("enabled")):
        return []

    problems = []

    recipients = cfg.get("recipients")
    if isinstance(recipients, str) or not isinstance(recipients, (list, tuple)):
        problems.append(
            "cs_agent_store_backup_recipients must be a list of age public keys "
            "(age1...), got %r." % (recipients,))
    elif not recipients:
        problems.append(
            "cs_agent_store_backup_recipients is empty: at least one age public "
            "key (age1...) is required. Keep the matching identity off the nodes.")
    else:
        for r in recipients:
            if not _AGE_RE.match(_text(r)):
                problems.append(
                    "cs_agent_store_backup_recipients: %r is not an age X25519 "
                    "public key (age1...)." % (r,))

    if not _cron_ok(cfg.get("freq")):
        problems.append(
            "cs_agent_store_backup_freq must be a 5-field cron expression or an "
            "@descriptor, got %r." % (cfg.get("freq"),))

    hours = _as_int(cfg.get("keep_all_hours"))
    if hours is None or hours < 0:
        problems.append(
            "cs_agent_store_backup_keep_all_hours must be a whole number >= 0, "
            "got %r." % (cfg.get("keep_all_hours"),))
    days = _as_int(cfg.get("retention_days"))
    if days is None or days < 1:
        problems.append(
            "cs_agent_store_backup_retention_days must be a whole number >= 1, "
            "got %r." % (cfg.get("retention_days"),))

    if not (_text(cfg.get("bucket")) or _text(cfg.get("export_bucket"))):
        problems.append(
            "no bucket: set cs_agent_store_backup_s3_bucket, or "
            "cs_agent_s3_export_bucket for it to fall back to.")

    endpoint = _text(cfg.get("endpoint")) or _text(cfg.get("export_endpoint"))
    if endpoint and not endpoint.startswith("https://"):
        problems.append(
            "the store backup endpoint (cs_agent_store_backup_s3_endpoint, else "
            "cs_agent_s3_export_endpoint) must be an https:// URL (or empty for "
            "AWS), got %r." % endpoint)

    for key in ("access_key", "secret_key"):
        own = _truthy(cfg.get(key + "_set"))
        source = ("store_backup_s3_%s" % key) if own else ("s3_export_%s" % key)
        if not own and not _truthy(cfg.get("export_" + key + "_set")):
            problems.append(
                "store_backup_s3_%s is not set, and there is no s3_export_%s "
                "to fall back to (vaulted secrets)." % (key, key))
        elif _truthy(cfg.get(("" if own else "export_") + key + "_placeholder")):
            problems.append(
                "%s is still the CHANGEME sample placeholder (vaulted secrets)."
                % source)

    return problems


class FilterModule(object):
    def filters(self):
        return {"cs_agent_store_backup_problems": cs_agent_store_backup_problems}
