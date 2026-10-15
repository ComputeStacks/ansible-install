"""Pure validation of cs-agent's backup export settings (backups.export.*).

tasks/export_check.yml builds one dict from the role's
`cs_agent_s3_export_*` variables and hands it to cs_agent_export_problems;
tests/cs_agent_export.yml feeds it canned dicts. Nothing here reads a host.

The rules are cs-agent's own (config/config.go, s3upload/s3upload.go
Config.Validate), checked here because the agent only LOGS a bad export
config at startup and then fails every export job, which nobody sees until a
customer asks for a download:

* the feature is inert until `bucket` is set, and nothing is checked then;
* the agent signs with static credentials, so both keys are required, and
  neither may be the sample secrets file's CHANGEME placeholder;
* an endpoint, when set, must be https (empty means real AWS);
* part_size_mb >= 5 (the S3 multipart minimum);
* default_ttl_sec <= max_ttl_sec;
* terminal task rows (tasks.retention_sec) must outlive the longest
  presigned URL, or a completed export whose link is still live is reaped.

Stricter than the agent where the agent's permissive value is a footgun: a
zero worker count, timeout, concurrency or TTL is refused rather than taken
to mean "none" or "no cap".

The dict carries `access_key_set` / `secret_key_set` and
`access_key_placeholder` / `secret_key_placeholder` booleans, never the keys,
so no message can echo a credential.
"""

import re

_INT_RE = re.compile(r"^[0-9]+$")

# s3types.ServerSideEncryption; "" sends no SSE header.
SSE_VALUES = ("", "AES256", "aws:kms", "aws:kms:dsse")

# key -> minimum value
_INT_MINIMUMS = (
    ("workers", 1),
    ("timeout_sec", 1),
    ("part_size_mb", 5),
    ("concurrency", 1),
    ("default_ttl_sec", 1),
    ("max_ttl_sec", 1),
)


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


def cs_agent_export_problems(cfg, tasks_retention_sec):
    """Every problem with an export config, as operator-facing sentences.

    An empty list means the config is valid, or that export is off (no bucket).
    """
    cfg = dict(cfg or {})
    if not _text(cfg.get("bucket")):
        return []

    problems = []

    if not _text(cfg.get("region")):
        problems.append("cs_agent_s3_export_region is empty.")
    for key in ("access_key", "secret_key"):
        if not _truthy(cfg.get(key + "_set")):
            problems.append(
                "s3_export_%s is not set (vaulted secrets)." % key)
        elif _truthy(cfg.get(key + "_placeholder")):
            problems.append(
                "s3_export_%s is still the CHANGEME sample placeholder "
                "(vaulted secrets)." % key)

    endpoint = _text(cfg.get("endpoint"))
    if endpoint and not endpoint.startswith("https://"):
        problems.append(
            "cs_agent_s3_export_endpoint must be an https:// URL (or empty for "
            "AWS), got %r." % endpoint
        )

    sse = _text(cfg.get("sse"))
    if sse not in SSE_VALUES:
        problems.append(
            "cs_agent_s3_export_sse must be one of %s, got %r."
            % (", ".join(repr(v) for v in SSE_VALUES), sse)
        )

    ints = {}
    for key, minimum in _INT_MINIMUMS:
        n = _as_int(cfg.get(key))
        if n is None or n < minimum:
            problems.append(
                "cs_agent_s3_export_%s must be a whole number >= %d, got %r."
                % (key, minimum, cfg.get(key))
            )
        else:
            ints[key] = n

    if "default_ttl_sec" in ints and "max_ttl_sec" in ints:
        if ints["default_ttl_sec"] > ints["max_ttl_sec"]:
            problems.append(
                "cs_agent_s3_export_default_ttl_sec (%d) must not exceed "
                "cs_agent_s3_export_max_ttl_sec (%d)."
                % (ints["default_ttl_sec"], ints["max_ttl_sec"])
            )

    retention = _as_int(tasks_retention_sec)
    if "max_ttl_sec" in ints and retention is not None:
        if ints["max_ttl_sec"] >= retention:
            problems.append(
                "cs_agent_s3_export_max_ttl_sec (%d) must be below cs-agent's "
                "task-row retention (tasks.retention_sec, %d): a completed "
                "export whose link is still live would be reaped."
                % (ints["max_ttl_sec"], retention)
            )

    return problems


class FilterModule(object):
    def filters(self):
        return {"cs_agent_export_problems": cs_agent_export_problems}
