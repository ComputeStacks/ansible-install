"""Fingerprint of a `DRY_RUN=1 cstacks seed` preview (the two-step attach).

Phase 1 of a two-step attach prints the preview and its fingerprint and stops;
phase 2 re-runs the preview and applies only when the fingerprint is the same
(`controller_seed_expect_preview_sha256`). The fingerprint is therefore only
useful if an unchanged manifest against an unchanged database prints the same
document twice. tests/controller_seed_preview.yml holds the cases.

What the preview is, from the controller source (app/services/bootstrap/,
lib/tasks/bootstrap_apply.rake, roles/controller/files/cstacks.sh):

* stdout of `rake bootstrap:apply` in a fresh `docker run` with no tty, so no
  carriage returns and no Rails log lines (production logs to a file unless
  RAILS_LOG_TO_STDOUT is set, and the seed container is not given it).
  Ruby/bundler warnings and failure text go to stderr, which is not hashed.
* Everything on it is written by Bootstrap::Recorder: a header naming the
  manifest path INSIDE the container (always /tmp/manifest.yml), one line per
  row in the apply's fixed section order and the manifest's own order, the
  database's order where the apply queries (`Region.order(:id)`,
  `find_each`), and a summary of counters. Secrets are printed as
  `«redacted»`. No timestamp, temp path, pid or host-specific value is
  printed. The catalog/products rake tasks a greenfield or full manifest runs
  print fixed strings.
* The whole apply really runs inside a transaction that is rolled back. Row
  ids come from PostgreSQL sequences, which a rollback does NOT rewind, so a
  row the preview CREATES gets a new id on every preview. That is the one
  run-specific value, and it reaches the output in exactly these places:

    - a create/seed diff line whose attribute is a foreign key to such a row:
      `region.metric_client_id` / `log_client_id` (a client created by the
      same apply), `dns_zone.provision_driver_id` and
      `product_module.primary_id` (a DNS driver created by the same apply);
      Recorder prints `<attr>: <before> → <id>`;
    - the same attributes on a drift line of a row that exists while the row
      it points at is new: `<attr>: database <x>, manifest <id>`;
    - the DNS product module link line: `→ ProvisionDriver #<id>`.

  The attach manifest (topology only) reaches none of them except
  `metric_client_id` / `log_client_id`, which there always name an EXISTING
  client and are stable. They vary with `controller_seed_full_manifest` or
  a greenfield preview (`controller_seed_update_addresses`).

So the normalization replaces the manifest-side integer of every `*_id`
attribute line, and the id in a `ProvisionDriver #<id>` link, with `<id>`,
and nothing else. It cannot tell a just-created row's id from an existing
one's in those positions, so a stable id there is normalized too; what that
gives up is noticing that a region would now point at a DIFFERENT existing
client with the same endpoint, which needs a client to be deleted and
recreated between the two phases. The database-side value of a drift line
and the before-side of a diff line are real database values and are kept,
as is every other number (`BillingResourcePrice #<id>` names a price that
already exists). Trailing carriage returns are dropped, in case the wrapper
ever runs under a tty.

One more thing reaches stdout that Recorder does not write: Sidekiq's client
logger, which prints a timestamped `INFO <time> pid=<n> tid=<x>: Sidekiq …
connecting to Redis` line (colored) the first time the apply touches a job
queue. It differs on every run, so log lines of exactly that shape -- an
optional color code, a level word, an ISO 8601 UTC time, `pid=` -- are
dropped before hashing. Found on the first smoke run of the two-step attach;
Recorder lines never start that way.
"""

import hashlib
import re

from ansible.errors import AnsibleFilterError

# Recorder#emit: thirteen spaces, `<attr>: <before.inspect> → <after.inspect>`.
_DIFF_ID = re.compile(r"^( {13}[a-z_]+_id: .* → )\d+$")
# Recorder#drift: `<attr>: database <current>, manifest <desired>`.
_DRIFT_ID = re.compile(r"^( {13}[a-z_]+_id: database .*, manifest )\d+$")
# Recorder#link from ApplyService#apply_dns_product_module.
_LINK_DRIVER = re.compile(r"^(  \[link  \] .* → ProvisionDriver #)\d+$")

# Sidekiq::Logger's pretty format, possibly colored by the terminal formatter.
_LOG_LINE = re.compile(
    r"^(?:\x1b\[[0-9;]*m)?(?:DEBUG|INFO|WARN|ERROR|FATAL)\s*(?:\x1b\[[0-9;]*m)?\s+"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+pid=\d+"
)

ID_PLACEHOLDER = "<id>"


def _lines(output):
    if output is None:
        return []
    if isinstance(output, (list, tuple)):
        return [str(line) for line in output]
    return str(output).splitlines()


def controller_seed_preview_normalize(output):
    """The preview with its run-specific values replaced, as a list of lines.

    Accepts the registered `stdout_lines` list or the `stdout` string.
    """
    out = []
    for line in _lines(output):
        line = line.rstrip("\r")
        if _LOG_LINE.match(line):
            continue
        for pattern in (_DIFF_ID, _DRIFT_ID, _LINK_DRIVER):
            line = pattern.sub(lambda m: m.group(1) + ID_PLACEHOLDER, line)
        out.append(line)
    return out


def controller_seed_preview_sha256(output):
    """sha256 (64 lowercase hex) of the normalized preview, one LF per line.

    Refuses an empty preview: every real one has at least the header and the
    summary, so empty means the preview did not run, and a fingerprint of
    nothing would match every other run that also previewed nothing.
    """
    lines = controller_seed_preview_normalize(output)
    if not any(line.strip() for line in lines):
        raise AnsibleFilterError(
            "controller_seed_preview_sha256: the DRY_RUN preview is empty; "
            "there is nothing to fingerprint"
        )
    document = "".join(line + "\n" for line in lines)
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


class FilterModule(object):
    def filters(self):
        return {
            "controller_seed_preview_normalize": controller_seed_preview_normalize,
            "controller_seed_preview_sha256": controller_seed_preview_sha256,
        }
