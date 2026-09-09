# v2 implementation contracts (frozen — Wave 0)

Binding for every implementer. The full plan lives outside the repo
(workspace doc); this file is the subset an implementer must not violate.
Change requests go to the engineering manager, not into the code.

## Vocabulary
`region` == controller `Location` (e.g. ams005). `az` == controller `Region`
(e.g. exm-005). Exactly ONE node per az — enforced by preflight, relied on
everywhere. The Location/Region mapping exists ONLY inside the seeding layer.

`app_domain` == the domain ONE az's load balancer answers on, and the CN of
the wildcard certificate it serves. An OPTIONAL inventory HOST var on a node,
defaulting to `cs_app_zone`. Every az has exactly one load balancer and in a
real estate every one of them answers on a different name, so this is per node
and never one environment-wide scalar. `cs_app_zone` is NOT retired by it and
does not become per-az: it stays the single parent tenant `Dns::Zone` — one
`pdnsutil create-zone`, one `dns.zones` entry — and every `app_domain` lives
at or under it.

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

### Per-az application domains (spellings are frozen — `playbooks/group_vars/all/app_domains.yml`)

Roles CONSUME these names. `cs_app_zone` keeps its meaning and stays
environment-wide; what splits off per az is the load balancer's domain.

| Name | Lives in | Shape | Cross-host read |
|---|---|---|---|
| `app_domain` | the INVENTORY, as a node host var | optional; a bare lowercase domain | it is a plain host var — `hostvars[h].app_domain` is fine |
| `cs_app_domain` | `playbooks/group_vars/all/app_domains.yml` | `app_domain \| default(cs_app_zone, true)` | **safe** — `hostvars[h].cs_app_domain` |
| `cs_app_wildcard_dir` | `playbooks/group_vars/all/app_domains.yml` | `app_wildcard_dir \| default('/var/lib/computestacks/.ssl_wildcard')` | **safe** |
| `cs_app_domains` | `roles/controller/vars/` **and** `roles/controller_seed/vars/` | sorted unique `cs_app_domain` over `cs_new_nodes` | **local only** |
| `cs_app_domain_cert_paths` | the same two role `vars/` files | domain -> pem path | **local only** |

**Where each one lives is part of the contract, not an accident.**
`cs_app_domain` is deliberately trivial — it reads only a plain inventory host
var and one global, no `hostvars` — because it is the one value in the feature
that MUST survive a cross-host read: the manifest is rendered on the
controller and needs every node's load balancer domain. Everything derived
from it is a map the rendering host indexes locally, which is hard rule 10's
shape and exactly why `cs_site` exists the same way. The two maps cannot move
into `playbooks/group_vars/`: their `cs_app_zone` entry is the legacy
certificate path, which is a `roles/controller` default and is not in scope in
the seeding play — and `add-region.yml` never runs that role's `main.yml` at
all.

`default(..., true)` — the second argument — is load-bearing. A plain
`default()` fires only on Undefined, so `app_domain: ""` would render a blank
load balancer domain, and `ValidateDomainWorker` returns early on a blank
domain rather than complaining. Empty means unset and takes the zone;
`roles/preflight` rejects it outright.

**The `cs_`-prefixed names in the two role `vars/` files are a deliberate
cross-role contract — do not "fix" them.** ansible-lint's production profile
wants a role's own vars prefixed with the role name, and both files carry
`# noqa: var-naming[no-role-prefix]` for these two. Prefixing them per role
would give one frozen contract two names in the two roles that MUST agree
about which domain maps to which pem — one writes those files, the other
slurps them back — and that disagreement is the precise thing the contract
exists to prevent. They are defined character-for-character identically in
both files; only the legacy branch differs, and each file says why.

#### Certificate path scheme

    domain == cs_app_zone  ->  the LEGACY path, unchanged
                               <controller_wildcard_dir>/sharedcert.pem
    otherwise              ->  <cs_app_wildcard_dir>/<domain>/sharedcert.pem

`cs_app_wildcard_dir` is the ONE spelling of the per-az root, and it is a
hostvars-free global precisely so it is in scope in the seeding play and in
attach mode. Neither role may re-derive that root from
`controller_wildcard_dir`: two role vars each carrying the same literal are
two literals that drift, and the drift is silent — the seed finds no pem, or
on a re-run a stale one, and the load balancer ends up serving a certificate
for a name it does not answer on. `roles/controller` is the one place both
names are in scope, so that role asserts they agree and names
`app_wildcard_dir` when they do not. `roles/controller_seed` has no
`controller_wildcard_cert` in scope, so its `cs_app_zone` entry uses
`controller_seed_shared_cert_path`, which carries the identical literal as its
own fallback and doubles as the attach-mode escape hatch.

**AN OPERATOR WHO MOVES `controller_data_dir` MUST SET TWO MORE VARIABLES**,
not one:

* `app_wildcard_dir` — where the per-az certificates are written AND read
  back. Without it `roles/controller` writes under the moved root while
  `roles/controller_seed` reads under the default one.
* `controller_seed_shared_cert_path` — the legacy `cs_app_zone` entry on the
  seeding side. Its fallback is the hard-coded default path, so a moved data
  directory leaves it pointing at a file that is not there.

`roles/controller` asserts the first. Nothing can assert the second from
inside the seeding play, which is why it is written down here.

**The legacy carve-out is load-bearing.** `Bootstrap::Writer#rotate!` skips a
credential whose DECRYPTED value already matches, so generate-once at a stable
path re-seeds as a genuine no-op. Move the default domain's certificate to a
new path and the value really does change, and the next converge pushes a
fresh certificate to every load balancer in the fleet — the exact fleet-wide
rotation the `creates:` guard exists to prevent. The default domain therefore
keeps the path v1 and v2 have always used.

#### The `LetsEncryptAuth#dns_zone` label walk

`LetsEncryptAuth#dns_zone` resolves a container's zone by walking **up** the
container's FQDN — the last 2 labels, then 3, then 4, then 5 — and taking the
first exact `Dns::Zone` match. The broadest zone that exists wins, which is
what lets ONE parent zone serve every load balancer domain beneath it.

Two consequences, and only two:

1. **`cs_app_zone` must be 2–5 labels.** A deeper zone is never reached by the
   walk and matches nothing.
2. **Every `app_domain` must be at or under `cs_app_zone`,** at a label
   boundary.

**`app_domain`'s own depth is NOT constrained.** The walk runs over the
container FQDN and stops at the first match, so a deep `app_domain` under a
3-label zone is perfectly fine — only `cs_app_zone`'s label count matters.
`roles/preflight` implements exactly these two checks, as hard asserts:
production's load balancer domains all live in one zone file, and the failure
they prevent — a tenant wildcard certificate that silently never issues — is
invisible at install time.

#### This change is CREATE-ONLY

`Bootstrap::ApplyService#apply_load_balancer` splits the row's fields two
ways, and the split decides the whole scope:

* `domain` goes in `attrs`, so on an EXISTING row `Writer#create_or_report!`
  reports drift and **never writes it**. No `addresses:` hash is passed, so
  `UPDATE_ADDRESSES=1` does not reach it either. A live load balancer's domain
  is operator-owned: humans edit it in the admin UI for years and a stale
  manifest must not roll that back.
* `shared_certificate` goes in `credential_secrets`, and `rotate!` **does**
  write it whenever the decrypted value differs.

So a per-az `app_domain` lands correctly when the `LoadBalancer` row is
CREATED — greenfield, and every new region attached — and applying it to an
already-seeded row would replace the certificate while leaving the domain
alone, handing that load balancer a certificate whose CN no longer matches the
name it still serves. Changing an existing load balancer's domain is
deliberately **out of scope** and stays a controller-side, human-operated
action.

`roles/controller_seed` therefore runs `DRY_RUN=1` **unconditionally** and
fails the run when the preview reports drift on a load balancer's `domain`
(gate G). `controller_seed_dry_run_first` governs only whether that preview is
PRINTED; it can no longer switch the gate off.

**Operator remediation when gate G fires:** change that load balancer's domain
in the admin UI first, then re-run the seed. Do not edit the inventory to
match the stale row unless the row is what you actually want — the inventory
is also what `roles/controller` names the certificate after.

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
