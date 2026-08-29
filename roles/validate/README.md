# validate

**Owner: Wave 4J.** `hosts: all` (greenfield) / `controller:nodes` (attach).

## Purpose

The last play of both playbooks. It changes nothing — every task is read-only
and `changed_when: false` — and asserts that the control-plane graph in
docs/contracts.md is actually wired, on the real addresses and with the real
credentials, from the hosts that use them.

The checks it runs are the ones whose failure is otherwise **silent**. A
ComputeStacks install can be green in every dashboard and still reject every
order because a prometheus label is wrong, or accept orders and never run a
backup because a node never latched its datachannel. Those are the failures
this role exists to catch at install time.

## The checks

| Name | Runs on | From | What it asserts |
| --- | --- | --- | --- |
| `services` | every host | itself | Every unit that host's groups imply is `systemctl is-active`; `haproxy` on nodes is `is-enabled` only. |
| `containers` | controller | itself | `portal` and `vault-bootstrap` exist and are running. |
| `agent` | nodes | **the controller** | cs-agent answers on `cs_ports.agent_http` at the address the controller actually dials. |
| `backfill` | nodes | **the controller** | `nodes.datachannel_backfilled_at` is set for this node. |
| `prometheus` | nodes | **the controller** | The controller's own placement query returns a non-zero count for this node's label set. |
| `borg` | nodes | itself | SSH to the backup server as `cstacks` runs the remote borg, and its version matches the client's. |
| `ssh` | nodes, registry | **the controller** | Root SSH from the controller succeeds. |
| `portal` | nodes | itself | `https://<cs_portal_domain>` answers. |
| `dns` | controller (once) | the controller | Every nameserver returns NS records for `cs_app_zone`. |

Each check is a task file included under its own tag, so

```bash
# just the label-contract query, on one region
ansible-playbook -i inventories/prod playbooks/site.yml \
  --tags prometheus --limit region_exm001

# everything except the two that need the backup server reachable
ansible-playbook -i inventories/prod playbooks/site.yml --tags validate \
  -e 'validate_skip=["borg"]'
```

`validate_skip` and `--skip-tags` do the same job; the list is there so an
environment can carry a permanent exclusion in its inventory.

## Why these checks are shaped the way they are

**The agent probe expects HTTP 401.** cs-agent v3.3.0 has no unauthenticated
route: every handler is wrapped in `requireCustomer`/`requireAdmin`, and a
request without an `Authorization` header is answered 401 by `authenticate`
(`httpapi/httpapi.go`). A 401 is therefore the strongest signal available —
it proves the port is reachable *from the controller* and that cs-agent, not
some other listener, is answering. Sending the real admin Bearer would prove
nothing more and would put a cleartext credential into a health check. A TCP
connect test would not distinguish cs-agent from anything else bound to 8500.

**The agent, prometheus and ssh checks all run from the controller**, not from
the ansible control machine. Those are control-plane rows #3, #10 and #4/#19,
and they fail for reasons — firewall source restrictions, tailnet routing,
basic-auth credentials — that only show up on the real leg. The agent probe
uses the same pairwise derivation `controller_seed` uses for
`node.agent_host`: the node's tailnet address only when the **controller** is
itself tailnet-joined, otherwise `primary_ip`.

**The prometheus check runs the controller's query, not a variant of it.**
`count(node_cpu_seconds_total{node="<hostname>",region="<az>",job="node-exporter"})`
is what `nodes/node_metrics.rb` matches on. An empty result set is the
failure that makes every node report 0 cpu / 0 mem and rejects every order,
while prometheus, the exporter and the scrape all look healthy.

**`systemctl is-active`, not `service_facts`.** `cs-firewall` is a oneshot
with `RemainAfterExit`; its sub-state is `exited`, which `service_facts`
surfaces as not-running. `is-active` reports what the unit actually is.

**The borg version comparison is blocking.** The client half of every backup
is the `cs-docker-borg` image, whose borg is `borg_version` — versions.yml
pins the image tag and that version together precisely so they cannot drift.
A server on a different version corrupts repositories in ways that only
surface at restore time. In attach mode this is the check that catches an
existing v1 server whose distro borg does not match, which is why the plan
calls it blocking; `validate_borg_version_compare: false` downgrades it to a
warning.

## Attach mode

`add-region.yml` runs this on `controller:nodes` only — the existing metrics
and backup hosts are not v2-built and their unit set is not this role's to
assert. On an `existing_env` controller the `cs-firewall` unit is dropped
from the expected service list (a v1 host runs `cs-iptables` instead);
everything else applies unchanged, because everything else is a property of
the control plane rather than of how the host was built.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `validate_skip` | `[]` | Check names to skip. |
| `validate_tls_verify` | `true` | Certificate verification for the prometheus query and the portal probe. Set false while the portal is on a self-signed certificate. |
| `validate_services_*` | see `defaults/main.yml` | Expected units, per group. |
| `validate_containers_controller` | `[portal, vault-bootstrap]` | |
| `validate_cstacks_bin` | `cstacks` | Absolute path if it is not on the controller's non-interactive PATH. |
| `validate_agent_probe_path` / `_expected_status` | `/v1/admin/changelog` / `401` | See above. |
| `validate_prometheus_*` | endpoint, credentials, metric, job | Endpoint and username follow `acme_web`'s. |
| `validate_borg_*` | key path, remote path, user | `validate_borg_remote_path` follows `backup_borg_remote_path`, which attach mode requires. |
| `validate_borg_version_compare` | `true` | Blocking by default. |
| `validate_portal_url` / `_status_codes` | `https://{{ cs_portal_domain }}/` | |
| `validate_ssh_timeout` / `validate_agent_timeout` | `10` | Seconds. |

Consumed, not owned: `cs_ports`, `hostname`, `primary_ip`, `az`,
`cs_portal_domain`, `cs_metrics_domain`, `cs_app_zone`, `dns_driver`,
`borg_version`, `borg_image`, `prometheus_basic_auth_password`,
`existing_env`, `tailscale_authkey` / `tailscale_enabled`.

## Requirements

`community.docker` (container state), pinned in `requirements.yml`. `dig`
comes from `dnsutils`, which `roles/common` installs on every host.

## Deviations from v1 (`roles/validate`)

* v1 checked only that a list of systemd units was running, per group, and
  called `cstacks test` on the controller. It never checked a single
  cross-host leg — not the agent, not the prometheus label contract, not
  borg, not the portal from a node — which is exactly where a ComputeStacks
  install goes wrong silently.
* v1 asserted `cadvisor` on the controller, metrics and registry hosts, none
  of which run it in v2 (or ran it usefully in v1).
* v1's consul check is gone with consul.
* `cstacks test` (`rake test_connection:all`) is not called here:
  `controller_post_enroll` already runs it, and it exits 0 even when a leg
  fails, so it is a report rather than an assertion. The legs it reports on
  are asserted individually above.
