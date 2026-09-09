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
| `services` | every host | itself | Every unit that host's groups imply is `systemctl is-active`; `haproxy` on nodes is `is-enabled` only. Asserts **nothing** on an `existing_env` host. |
| `containers` | controller | itself | `portal` and `vault-bootstrap` exist and are running. |
| `agent` | nodes | **the controller** | cs-agent answers on `cs_ports.agent_http` at the address the controller actually dials. |
| `backfill` | nodes | **the controller** | `nodes.datachannel_backfilled_at` is set for this node. |
| `prometheus` | nodes | **the controller** | The controller's own placement query returns a non-zero count for this node's label set, against **this node's site's** prometheus, with **that site's** credentials. |
| `borg` | nodes | itself | SSH to **this node's site's** backup server as `cstacks` runs the remote borg, and its version matches the client's. Skipped when that site has no backup server. |
| `ssh` | nodes, registry | **the controller** | Root SSH from the controller succeeds. |
| `portal` | nodes | itself | `https://<cs_portal_domain>` answers. |
| `acme_backend` | nodes | itself | The controller's ACME backend answers at the exact `regions[].acme_server` address this az's manifest carries. |
| `lb_domain` | nodes (new azs only) | **the controller** | This az's `LoadBalancer` reached `domain_valid`, and its `domain` equals the CN of its `shared_certificate`. |
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

**The ACME back-channel check recomputes the manifest's own expression.**
Control-plane row #7 is the one leg with no other alarm on it: the node's
haproxy proxies tenant ACME challenges to the controller's portal on
`cs_ports.controller_acme_backend`, at whatever address
`regions[].acme_server` carries for that az. `controller_seed` derives that
address pairwise — the controller's tailnet address only when both ends are
paired, otherwise its `primary_ip` — so a remote region with no tailnet gets
an address that is routable on the controller's LAN and nowhere else. Nothing
fails at install time; the first customer certificate fails weeks later.
`vars/main.yml` therefore rebuilds the *same* expression (a variant would
validate an address the manifest does not carry) and the node probes it. Any
HTTP response counts as reachable — the portal answers `:3000` directly, and
what is being asserted is routing. The fail message names the az, the derived
address, how it was derived, and the `controller_acme_address` host var that
overrides it.

**The load balancer domain check has three outcomes, and only one of them
fails.** Every az's load balancer answers on its own `app_domain`
(`playbooks/group_vars/all/app_domains.yml`) and serves a wildcard
certificate whose CN is that name. Nothing else in a run closes the loop
between the inventory and the public DNS: preflight checks the inventory
against itself, `roles/controller` writes a certificate, `controller_seed`
writes a manifest, and all three can agree while `*.<app_domain>` does not
exist. The controller *does* check it —
`LoadBalancerWorkers::ValidateDomainWorker` runs from the row's `after_save`
and writes `domain_valid` — but asynchronously, and nothing fails when it
comes back false: a missing wildcard CNAME passes preflight, passes the seed,
ends the run green, and surfaces weeks later as one region with no working
ingress.

`domain_valid` is a plain boolean and is `false` both before the worker has
run and after it has run and failed, so asserting on it alone would go red on
every install that finished quicker than sidekiq. `domain_valid_check` is the
discriminator — the worker writes both columns in one `update_columns` and
nothing else writes it:

| `domain_valid_check` | `domain_valid` | Outcome |
| --- | --- | --- |
| empty | — | **reported, not asserted**: the verdict does not exist yet |
| set | `false` | **fail**, with the two DNS records and the event codes |
| set | `true` | pass |

The task waits up to `validate_lb_domain_retries` × `_delay` for the verdict
rather than warning at once, and stops on the first non-pending answer — a
healthy install pays for one query.

If the verdict is still pending when the ceiling is reached, ansible prints
`Task failed: Action failed.` before this task's own "not a failure" report.
That line is ansible saying the `until` condition never came true;
`failed_when: false` keeps it from failing the play. Expect it on a first
`add-region.yml` run — the controller has usually not finished validating by
the time validate gets here — and re-run `--tags lb_domain` rather than
reading it as a broken install.

The failure message names the exact pair of records the controller demands:
`<app_domain>` A to that load balancer's public IP, and `*.<app_domain>`
**CNAME** to `<app_domain>`. The wildcard cannot be an A record:
`ValidateDomainService` queries a random label under the domain for a CNAME,
so an A record answers nothing and fails with event_code
`2721edc59787a807`.

**It does not assert that the row's `domain` equals `cs_app_domain`.** A load
balancer's domain is operator-owned controller-side — it is edited in the
admin UI and the apply deliberately never rewrites it on an existing row — so
asserting equality would turn a supported edit into a permanently red
validate. The case that matters, a manifest whose domain disagrees with a row
it cannot rewrite, is caught before anything is written by gate G in
`roles/controller_seed`. The CN comparison is asserted, because CN and domain
are seeded from the same value and cannot legitimately disagree on a row this
run created.

The check runs only for hosts in `cs_new_nodes`. An `existing_env` region's
load balancer was never in this run's manifest, and its DNS is not this run's
claim to make.

**Every per-site value comes from the site maps, never from a global.** The
prometheus query dials `cs_site_metrics_domains[cs_site]` with the
`cs_site_metrics_credentials[cs_site]` pair, and the borg check SSHes to
`cs_site_backup_hosts[cs_site]` — the frozen spellings from
`playbooks/group_vars/all/sites.yml` (docs/contracts.md §Site scoping),
resolved on the host being validated. `validate_backup_host` used to be
`groups['backup'] | first`, which in a two-site fleet handed every node
whichever backup server sorted first: a node in one site then SSHed at another
site's server, failed on a key that was never installed there, and named the
wrong host in the failure message. `validate_prometheus_endpoint` had the same
shape via the single global `cs_metrics_domain`.

**Both halves of the basic-auth pair are per site, not just the password.**
The username used to be `acme_web`'s v2 default (`promuser`) everywhere. A
metrics VM built by v1 answers to whatever its own htpasswd holds, and a wrong
username is a 401 that is *indistinguishable* from a wrong password — same
status, same symptom, and a fail message that would have sent the operator
looking at labels, scrape targets and firewall rules. The query task is
`no_log` for the same reason: both halves now come out of the vaulted
`metrics_site_credentials`, and `uri`'s own argspec redacts `url_password` but
not `url_username`. The assert that follows carries the status, the transport
error, the endpoint and the label set, so nothing diagnostic is lost.

A single-site inventory sets `site` nowhere, every host lands in the site
called `default`, and all three resolve to exactly the values they always did.

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
assert.

`lb_domain` is the check attach mode most needs, and it runs there unchanged:
the new az is in `cs_new_nodes`, its `app_domain` is required to differ from
`cs_app_zone` (`roles/preflight`), and its wildcard CNAME is the record most
likely to have been forgotten when a region is added years after the
environment was built. No `existing_env` region is checked — those load
balancers were never in this run's manifest.

**That principle applies to the existing controller too, and the `services`
check therefore asserts nothing at all on any `existing_env` host.** It used to
drop only `cs-firewall` and go on asserting `docker`, `nginx`, `postgresql`,
`redis-server` and `prometheus-node-exporter` there. Those are v2's unit names.
The production controller was built by the v1 provisioner, years ago, on
Debian: it may run postgres in a container rather than as `postgresql.service`,
it may carry the upstream node_exporter as `node_exporter.service` rather than
Debian's `prometheus-node-exporter`, and its nginx may be a container. Each of
those is a healthy controller that the old list called a failure — and a run
that ends `failed` on a healthy host is worse than no check, because the
operator cannot tell it apart from the real failures this role exists to catch.

Nothing is lost, because attach mode writes no unit to that host (see
docs/attach-mode.md §What it writes) and every unit the list named is already
proved to work functionally by a check that still runs:

| Old assertion | What still proves it |
| --- | --- |
| `docker` | `containers` finds `portal` and `vault-bootstrap` running. |
| `postgresql` | `controller_seed`'s `bootstrap:apply` read and wrote the database before `validate` started. |
| `nginx` | `portal` dials `https://<cs_portal_domain>/`. |
| `redis-server` | the `portal` container does not boot without it. |
| `prometheus-node-exporter` | `prometheus` runs the controller's placement query for the new node. |

The `containers` check is **not** relaxed: `portal` and `vault-bootstrap` are
v1's own container names (`roles/vault` calls the storage layout "v1 layout —
do not change" precisely because attach mode reads an existing controller's
keys out of it), so both are correct on a v1 controller and both are things
this run depends on.

Everything else applies unchanged on an existing host, because everything else
is a property of the control plane rather than of how the host was built.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `validate_skip` | `[]` | Check names to skip. |
| `validate_tls_verify` | `true` | Certificate verification for the prometheus query and the portal probe. Set false while the portal is on a self-signed certificate. |
| `validate_services_*` | see `defaults/main.yml` | Expected units, per group. All of them are skipped on an `existing_env` host. |
| `validate_containers_controller` | `[portal, vault-bootstrap]` | |
| `validate_cstacks_bin` | `cstacks` | Absolute path if it is not on the controller's non-interactive PATH. |
| `validate_agent_probe_path` / `_expected_status` | `/v1/admin/changelog` / `401` | See above. |
| `validate_prometheus_endpoint` | `https://{{ cs_site_metrics_domains[cs_site] \| default(cs_metrics_domain) }}:{{ cs_ports.metrics_prometheus }}` | This host's site's metrics vhost. |
| `validate_prometheus_username` / `_password` | this host's site's entry in `cs_site_metrics_credentials`, else `cs_metrics_credentials_default` | Both halves matter — a wrong username is an indistinguishable 401. |
| `validate_prometheus_metric` / `_job` | `node_cpu_seconds_total` / `node-exporter` | The controller's own label contract. |
| `validate_lb_domain_retries` / `_delay` | `12` / `10` | Ceiling on the wait for the controller's asynchronous domain verdict — two minutes. `0`, not `1`, takes whatever answer is there: ansible counts *re*-tries, so `1` is two attempts. |
| `validate_borg_*` | key path, remote path, user | `validate_borg_remote_path` follows `backup_borg_remote_path`, which attach mode requires. |
| `validate_borg_version_compare` | `true` | Blocking by default. |
| `validate_portal_url` / `_status_codes` | `https://{{ cs_portal_domain }}/` | |
| `validate_ssh_timeout` / `validate_agent_timeout` / `validate_acme_timeout` | `10` | Seconds. |

Computed in `vars/main.yml`, not overridable: `validate_backup_host`
(`cs_site_backup_hosts[cs_site]`, empty when this site has no backup server),
`validate_metrics_credentials` (`cs_site_metrics_credentials[cs_site]`, else
`cs_metrics_credentials_default`), `validate_expected_services` /
`_enabled_services` / `_containers`, and the tailnet and ACME derivations.

Consumed, not owned: `cs_ports`, `hostname`, `primary_ip`, `public_ip`,
`az`, `cs_portal_domain`, `cs_metrics_domain`, `cs_app_zone`,
`cs_app_domain` and `cs_new_nodes`
(`playbooks/group_vars/all/app_domains.yml`, `…/sites.yml`), `dns_driver`,
`borg_version`, `borg_image`, `existing_env`, `tailscale_authkey` /
`tailscale_enabled`, `controller_acme_address`, and the frozen site contract
`cs_site`, `cs_site_backup_hosts`, `cs_site_metrics_domains`,
`cs_site_metrics_credentials`, `cs_metrics_credentials_default`
(`playbooks/group_vars/all/sites.yml`).

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
