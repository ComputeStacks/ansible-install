# v2 implementation contracts (frozen — Wave 0)

Binding for every implementer. The full plan lives outside the repo
(workspace doc); this file is the subset an implementer must not violate.
Change requests go to the engineering manager, not into the code.

## Vocabulary
`region` == controller `Location` (e.g. ams005). `az` == controller `Region`
(e.g. exm-005). Exactly ONE node per az — enforced by preflight, relied on
everywhere. The Location/Region mapping exists ONLY inside the seeding layer.

`site` == the physical facility a host lives in. A provisioner-only concept:
the controller has no column for it, and nothing about it is ever seeded. One
metrics host and AT MOST one backup server per site (zero backup servers means
that site's nodes install without backups). A site holds one or more `region`s;
a `region` never spans sites. It is neither Region nor Location, and that is
the whole point — a real estate can have two different Locations sharing one
metrics server, and no controller-side grouping expresses that. Set as an inventory HOST var (`site`) on nodes, metrics
hosts and backup hosts; unset everywhere means the single site called
`default`, which is what every inventory that predates this concept is.

## Hard rules
1. **Inventory vars only in shared-host and seed templates.** Never gathered
   facts (`ansible_hostname`, `ansible_default_ipv4`, …) — under `--limit`,
   un-targeted hosts have no facts and their regions silently vanish from
   rendered config. Required node vars: `hostname`, `primary_ip`, `public_ip`,
   `region`, `az`, `container_network`, `container_network_name`. Metrics and
   backup hosts additionally require `site` — but ONLY when the inventory
   holds more than one site, so a single-site inventory needs no edit. Nodes
   take `site` too; a node that omits it falls into the site called `default`,
   which preflight catches as "site `default` holds nodes but no metrics
   host" the moment any other host names a real site.
2. **Never index `[0]` in templates.** Iterate groups. (Delegating an ACTION
   to `groups['controller'][0]` is fine — single controller is architectural.)
3. **Convergent roles.** No `when: service is not defined` install guards.
   A version bump in versions.yml must roll the change on rerun. Handlers
   restart services on config change.
4. **Every version pinned** in `playbooks/group_vars/all/versions.yml`. No
   `latest`, `stable`, `main` tags. Items marked VERIFY are the owning wave's
   DoD.
5. **Cross-host ports come from `cs_ports`** (ports.yml) — any port another
   host dials or the firewall opens. Loopback-only role-internal ports (e.g.
   vault 8200) may live in role defaults. No literal port numbers in templates.
6. **Public repo hygiene.** No real hostnames, IPs, or credentials in tracked
   files — RFC 5737/1918 examples only. `workspace/` is gitignored scratch.
7. **Firewall:** dedicated nftables table via role-owned file + unit. NEVER
   `flush ruleset` (destroys docker's and the agent's `cs_agent` tables; docker
   does not recover until dockerd restarts). No forward-hook drop chain. Do not
   create v1's `expose-ports`/`container-inbound` chains.
8. **SECRET_KEY_BASE / USER_AUTH_SECRET are immutable inputs.** Never
   generated, never re-rendered on existing controllers. Preflight fails on
   blank/short.
9. **Attach mode (`existing_env: true`):** the inventory is a PARTIAL view of
   the fleet — no shared-host file rendered whole. Additive fragments and
   lineinfile appends only. Whole-file exceptions: the v2 `cstacks` script and
   NEW per-az file_sd fragment files. `/etc/default/computestacks` is
   append-only, always.
10. **A variable defined in `playbooks/group_vars/` that references
    `hostvars` may only be read on the host currently executing.** NEVER
    `hostvars[h].<that var>`. See §Variable scope — this one fails silently,
    which is why it is a rule and not a style note.

## Ownership map (role -> wave -> owner)
Wave 1A: preflight, common, ssh_trust, node_kernel
Wave 1B: vault, docker_config, docker_tls
Wave 1C: (controller repo) bootstrap:apply rake + service + specs + schema doc
Wave 2D: cs_agent, haproxy, node_observability, backup_server
Wave 2E: metrics, loki
Wave 2F: firewall, tailscale, ubuntu_pro
Wave 3G: controller, acme_web, registry
Wave 3H: powerdns
Wave 4I: controller_seed, controller_post_enroll
Wave 4J: validate, site.yml/add-region.yml final wiring, attach_fragments,
         controller attach_prep, docs/
Single owners: `ops` docker network -> metrics role. `/etc/update-motd.d/
50-computestacks` -> common. `/etc/modules-load.d/cs-firewall.conf` -> firewall;
`/etc/modules-load.d/cs-node.conf` -> node_kernel (separate files, no sharing).

## Cross-wave interface contracts

### Variable scope (hard rule 10)

A variable defined in `playbooks/group_vars/` that references `hostvars` may
only be read on the host currently executing:

    cs_site_metrics_hosts[cs_site]                # this host's site
    cs_site_metrics_hosts[hostvars[h].cs_site]    # another host's site

NEVER `hostvars[h].cs_site_metrics_hosts`. When ansible lazily templates
another host's variable, `hostvars` is not in scope: the expression evaluates
to Undefined, and any `| default(...)` guard then converts that into a
wrong-but-plausible value — no error, no warning, nothing in a drift report.
Verified on this repo's ansible-core: `hostvars['node2001'].cs_site_metrics_hosts`
returns Undefined while `cs_site_metrics_hosts` in the same play returns the
full map.

This is not hypothetical. The first draft of the site-scoping work read
`hostvars[h].cs_site_metric_endpoint` and would have pointed a new region at
another site's prometheus and loki — logs and metrics to the wrong facility, and
nothing anywhere would have said so.

The same rule has a second face, and it bites any playbook written outside
`playbooks/`: **a var supplied through a play's `vars_files:` is play scope,
not host scope, so it never enters another host's `hostvars`.** Load these
names through `group_vars/` or every map built with
`map('extract', hostvars, 'cs_site')` fails with `object of type
'HostVarsVars' has no attribute 'cs_site'`. That is why `tests/group_vars` is
a symlink to `playbooks/group_vars` rather than a `vars_files:` list — the
harness then loads exactly what the real playbooks load, with nothing to keep
in sync.

A variable that references only `groups`, plain inventory host vars, and other
hostvars-free variables IS safe to read cross-host — that is exactly why
`cs_site` exists as its own trivial variable rather than being folded into the
maps. Keep the same shape when you add one: the value read across hosts stays
hostvars-free, and anything needing `hostvars` becomes a map that the
rendering host indexes locally.

### Site scoping (spellings are frozen — `playbooks/group_vars/all/sites.yml`)

Roles CONSUME these names. Do not re-derive any of them inline, the same way
the tailnet-membership predicate has exactly one spelling.

| Name | Shape | Cross-host read |
|---|---|---|
| `cs_site` | this host's site, `site` or `default` | **safe** — `hostvars[h].cs_site` |
| `cs_site_metrics_hosts` | site -> metrics inventory_hostname | local only |
| `cs_site_backup_hosts` | site -> backup inventory_hostname (site may be absent = no backups) | local only |
| `cs_site_metrics_domains` | site -> `metrics_domain` of that site's metrics host, else `cs_metrics_domain` | local only |
| `cs_existing_hosts` | hosts flagged `existing_env` truthy | local only |
| `cs_new_nodes` | `groups['nodes']` minus the above, unordered — `\| sort` at the point of use | local only |
| `cs_metrics_credentials_default` | the four environment-wide basic-auth values as one dict | safe |
| `cs_site_metrics_credentials` | site -> the same four, resolved per site | safe |

Per-site nginx basic-auth credentials have ONE spelling, and the fallback goes
on the SUBSCRIPT, never on the field:

    (cs_site_metrics_credentials[<site>] | default(cs_metrics_credentials_default)).loki_password

Only sites named in the optional vaulted `metrics_site_credentials` appear in
the map; every other site resolves entirely to the defaults. Guarding the
field instead (`…[<site>].loki_password | default(x)`) swallows a
misconfigured entry along with the absent one.

`cs_existing_hosts` uses a two-stage `selectattr('existing_env', 'defined')`
then `selectattr('existing_env')`, NOT `rejectattr('existing_env', 'defined')`:
the latter also drops a host that explicitly says `existing_env: false`, which
every other consumer in the repo (`existing_env | default(false)`) counts as
new.

### Controller invocation helper (used by cs_agent enroll; provided by cstacks)
The v2 `cstacks` CLI provides:
    cstacks runner '<ruby one-liner>'
which executes `bin/rails runner` inside the running `portal` container
(`docker exec portal bin/rails runner ...`), stdout passed through, exit code
propagated. The cs_agent role's enroll task runs, delegated to the controller
host:
    cstacks runner "puts Node.find_by!(hostname: %q{<node hostname>}).agent_token_hash"
and writes the 64-hex result into `metadata.admin_token_hash` in
`/etc/computestacks/agent.yml`, then restarts cs-agent. Empty/non-hex output =
fail the task loudly. Until Wave 3G lands, cs_agent may stub this behind a
tagged task and note it in its report.

### agent.yml (cs_agent role owns; schema is cs-agent v3.3.0)
- `metadata.listen_addr`: `:8500` when the node is tailnet-joined, else
  `<primary_ip>:8500`. NEVER the tailscale IP (containers dial
  metadata.internal -> primary_ip).
- Key is `backups.borg.compression` (upstream sample's `compress` is a bug);
  `mariadb.*` is TOP-LEVEL (sample nests it wrongly); omit dead keys
  (`computestacks.host` optional, `backups.export.cleanup_freq`,
  `failed_retention_sec`).
- borg ssh keyfile MUST live under `/etc/computestacks/` (only dir bind-mounted
  into the borg container).

### Facts exception (blessed): tailscale_ip
The ONE sanctioned use of non-inventory data in cross-host templates:
`hostvars[h].tailscale_ip | default(hostvars[h].ansible_local.computestacks.tailscale_ip | default(''))`
(both defaults mandatory). The middle term requires the repo's persistent
fact cache (`ansible.cfg`: `fact_caching`, `.ansible_facts_cache/`); a fresh
clone must converge each host once before a `--limit`ed render can see that
host's tailnet address. The tailscale role owns the `tailscale_ip` key in
`/etc/ansible/facts.d/computestacks.fact` and MERGES into that file, never
overwrites. Tailnet MEMBERSHIP remains a pure-inventory predicate — only the
address VALUE uses this exception. The membership predicate has exactly ONE
spelling, everywhere, in tasks and templates alike:

    ((tailscale_authkey | default('')) | length > 0)
      and (tailscale_enabled | default(true) | bool)

(`hostvars[h].` in front of both terms when asking about another host). Not
`tailscale_authkey is defined`: an authkey defined as an empty string is NOT
tailnet membership, and a mixture of spellings makes roles disagree about the
same host — the agent binding `:8500` on all interfaces while the firewall,
metrics and manifest all treat the node as off-tailnet.

### Container network placement (input-chain enforceability)
Node-side containers (cadvisor, fluentd) run with --network=host, listeners
bound to primary_ip (cadvisor) / 127.0.0.1 (fluentd's docker-log-driver port) —
published-port DNAT bypasses the input chain, host networking doesn't, and the
fleet already runs these host-net. Metrics-host containers bind 127.0.0.1
behind nginx. Galaxy roles install to galaxy_roles/ (gitignored):
`ansible-galaxy role install -r requirements.yml -p galaxy_roles` and
`ansible-galaxy collection install -r requirements.yml -p collections`.

### Tailscale address derivation (PAIRWISE — never per-node)
- Manifest `node.agent_host` = node tailscale IP ONLY if the CONTROLLER is
  tailnet-joined; else omit (controller dials primary_ip).
- Prometheus scrape address for a node = tailscale IP ONLY if the METRICS host
  is tailnet-joined; else primary_ip/public route.
- Enrollment/cstacks-runner is controller-local — unaffected.

### Prometheus contract (metrics role + validate)
file_sd labels per node target: `region: <az>`, `node: <hostname>`;
`job_name: node-exporter` (also `cadvisor`, `haproxy` jobs as v1). The
controller's placement queries use exactly
`node="<hostname>",region="<az>",job=~"node-exporter"` — wrong labels means
every node reports 0 cpu/0 mem and ALL orders are rejected. validate asserts a
non-empty query result per node.

### Manifest (controller_seed renders; schema owned by controller repo)
Schema doc: `doc/bootstrap_manifest.md` in the controller repo (Wave 1C
deliverable; FROZEN after manager review — Wave 4I builds against it).
Sections omittable. Secrets-bearing file: root 0600, deleted after successful
apply. `cstacks seed` wraps `rake bootstrap:apply[<path>]`; `DRY_RUN=1`
supported and used as the attach-mode gate.

### Final wiring (Wave 4J) — decisions that closed the plan

Recorded here because they change what the ownership map above says, or
resolve a flag an earlier wave raised. Nothing above is amended; this is the
delta.

1. **`attach_fragments` was not built.** The plan gave Wave 4J a thin glue
   role composing the attach entry points per host group. Written out, it was
   one role whose entire body was `when: 'metrics' in group_names` branches
   around three `include_role` calls — strictly worse than the plays that
   already scope by `hosts:`. The entry points are called directly from
   `playbooks/add-region.yml`, one play per host group. Ignore
   `attach_fragments` in the Wave 4J ownership line.
2. **`node_exporter` is a new role** (`roles/node_exporter`, Wave 4J), on
   `hosts: all`. The plan assigned it to "a community role in the all-hosts
   play", but no such role was ever pinned, and both consumers (`metrics`
   scrapes it everywhere, `firewall` opens 9100 everywhere) were already
   built. Distro package, held, as the plan requires.
3. **`add-region.yml` runs the whole `docker_tls` role on the new node**, not
   `tasks_from: issue`. `tasks/main.yml` routes through `issue.yml` — which
   is the piece that reaches the existing controller's vault — and then
   installs the listener drop-in, without which the certificate sits on disk
   and the controller has nothing to dial. The `issue` entry point remains
   for callers that only want issuance; `tests/roles.yml` keeps it parsed.
4. **The metrics/loki containerization style stays as built** (hand-rolled
   systemd units wrapping `docker run --rm`) alongside `vault`'s
   `community.docker.docker_container`. Wave 2E flagged the inconsistency;
   both satisfy rule 3, and unifying them means rewriting three working
   roles for style. **Punted deliberately** — if it is ever unified, the
   `docker_container` form is the one to keep, and it is one change across
   `metrics`, `loki` and `node_observability` at once, not a drive-by.
5. **`loki_image` and `fluentd_loki_image` stay paired at 2.9.10**, as Wave
   2D and 2E both asked. `versions.yml` carries the pairing note on both
   entries; they move together or not at all.
6. **The docker engine packages and `node_exporter_apt_version` are pinned**
   like everything else (rule 4). They were left empty during the wave on the
   belief that neither upstream published for Ubuntu 26.04; re-checked
   2026-08-28, both do — `download.docker.com/linux/ubuntu` has a `resolute`
   suite (`resolute` IS 26.04; 25.10 is `questing`) and the 26.04 archive
   ships `prometheus-node-exporter`. `versions.yml` carries the exact
   strings and the verification date. Both packages are still `apt-mark
   hold`-ed after install, which is what v1 actually relied on, and the holds
   are released before each install so a pin bump still rolls through.
7. **`playbooks/vars/Ubuntu-26.yml`** supplies the platform variables
   `geerlingguy.postgresql` 4.0.0 is missing for Ubuntu 26 — its first task
   is an `include_vars` that would otherwise fail outright. `include_vars`
   falls back to the playbook directory, so this needs no fork of the pinned
   role.
8. **`postgres_repo` is a new role** (`roles/postgres_repo`, Wave 4J), on the
   controller and the nameservers, before `geerlingguy.postgresql` and
   `powerdns` respectively. Ubuntu 26.04 ships PostgreSQL 18 and carries no
   `postgresql-17`, which is the major the controller's schema and CI are
   validated against, so the pinned major comes from PGDG with an apt
   preferences pin. `add-region.yml` does not run it: attach mode installs no
   postgres package anywhere. Resolves the `postgresql_version` VERIFY.
9. **The eight per-wave syntax harnesses are one file**, `tests/roles.yml`.
   They existed because `site.yml` did not parse yet. It does now, so the
   harness keeps only what the two playbooks cannot cover: every role in
   isolation and every reusable `tasks_from:` entry point.

## Definition of done (every provisioner wave)
`ansible-lint roles/<role>` clean (new .ansible-lint at repo root, production
profile); `ansible-playbook --syntax-check` of a minimal per-role test play if
site.yml doesn't parse yet; role README.md (purpose, vars, owner); no writes
outside your allowlist; STOP AND REPORT rather than guess on any ambiguity —
that is a successful outcome, silent guessing is not.
