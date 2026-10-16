# Design contracts

The rules every role in this repository follows, and the interfaces between
roles that must not drift. It is written for anyone changing the provisioner;
to run it, start with the [README](../README.md) and
[install.md](install.md) instead.

Code comments cite these rules by number ("contracts rule 3") and by section
("§Variable scope"). Keep the numbering stable, and change a rule in the same
commit as the code that needs the change.

## Vocabulary

`region` == controller `Location` (e.g. exm001). `az` == controller `Region`
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
   `region`, `az`, `container_network`, `container_network_name`,
   `docker_data_device` (the blank data disk, an absolute path under `/dev/`;
   roles/node_storage). Metrics and backup hosts additionally require `site`
   — but ONLY when the inventory holds more than one site, so a single-site
   inventory needs no edit. Nodes take `site` too; a node that omits it falls
   into the site called `default`, which preflight catches as "site `default`
   holds nodes but no metrics host" the moment any other host names a real
   site. The one exception is §Facts exception.
2. **Never index `[0]` in templates.** Iterate groups. (Delegating an ACTION
   to `groups['controller'][0]` is fine — a single controller is
   architectural.)
3. **Convergent roles.** No `when: service is not defined` install guards.
   A version bump in `versions.yml` must roll the change on a re-run, and
   handlers restart services on config change.
   *One exception, on live nodes only* (a node with tenant containers, per
   `roles/node_upgrade` `tasks_from: detect_live`): a `docker_apt_packages`
   bump does NOT roll there on a re-run of `site.yml` or `add-region.yml` —
   those skip docker's packages on that node and warn — and a pending reboot
   is not taken there either. Both roll through `make upgrade-docker
   ENV=<env>` (`playbooks/upgrade-docker.yml`) in a maintenance window,
   because restarting dockerd or rebooting stops tenant containers that have
   no restart policy. A node that is not live, and every other host, still
   converges on re-run.
4. **Every version pinned** in `playbooks/group_vars/all/versions.yml`. No
   `latest`, `stable` or `main` tags. A bump notes the date and source it was
   verified against.
5. **Cross-host ports come from `cs_ports`** (`ports.yml`) — any port another
   host dials or the firewall opens. Loopback-only role-internal ports (e.g.
   vault 8200) may live in role defaults. No literal port numbers in
   templates. The one per-host port is sshd's: the `ssh_port` host var, with
   `cs_ports.ssh` as its default, read off the host that listens on it.
   [control-plane.md](control-plane.md) is the graph the ports are derived
   from; change both together.
6. **Public repo hygiene.** No real hostnames, IPs or credentials in tracked
   files — RFC 5737/1918 addresses and `example.com` names only. Real
   inventories and notes live in gitignored paths.
7. **Firewall:** a dedicated nftables table via a role-owned file and unit.
   NEVER `flush ruleset` (it destroys docker's and the agent's `cs_agent`
   tables; docker does not recover until dockerd restarts). No forward-hook
   drop chain. Do not create v1's `expose-ports`/`container-inbound` chains.
8. **SECRET_KEY_BASE / USER_AUTH_SECRET are immutable inputs.** Never
   generated, never re-rendered on existing controllers. Preflight fails on a
   blank or short one.
9. **Attach mode (`existing_env: true`):** the inventory is a PARTIAL view of
   the fleet — no shared-host file is rendered whole. Additive fragments and
   lineinfile appends only. Whole-file exceptions: the v2 `cstacks` script,
   NEW per-az `file_sd` fragment files, and — only when the operator sets
   `backup_server_sshd_manage` — the backup server's sshd drop-in, rendered
   from operator-supplied fleet values. `/etc/default/computestacks` is
   append-only, always. See §Attach mode.
10. **A variable defined in `playbooks/group_vars/` that references
    `hostvars` may only be read on the host currently executing.** NEVER
    `hostvars[h].<that var>`. See §Variable scope — this one fails silently,
    which is why it is a rule and not a style note.

## Play ordering

The order of plays in `site.yml` is load-bearing:

* **Reachability, then preflight, run first, on every host, and change
  nothing.** Every targeted host must answer a ping before anything gathers
  facts (`roles/preflight/tasks/reachable.yml`, the first play of every entry
  point), because ansible otherwise drops an unreachable host and runs every
  later play without it. Every check that can fail on inventory or host
  state belongs in preflight, and the preflight play is `any_errors_fatal`,
  so a bad input on any one host stops the run before any host is touched.
* **Every play stops on the first failed or unreachable host** (`ansible.cfg`
  `any_errors_fatal = True`), except the validate plays, which change
  nothing. A partly converged fleet that carries on is worse than a stopped,
  idempotent run.
* **`node_storage` runs before docker is installed** on the nodes; docker or
  containerd starting first would write their state to the root disk. Its
  read-only probe also runs in preflight and in `upgrade-docker.yml`.
* **`controller_seed` → `cs_agent` → `controller_post_enroll`.** Seeding
  creates the Node rows and mints the agent tokens, the nodes then enroll
  against those rows, and the backfills call every node's agent, which
  answers 401 until it is enrolled. They are separate roles for exactly this
  reason ([control-plane.md](control-plane.md), "The graph fixes the play
  order").
* **`validate` runs last.**

`add-region.yml` keeps the same relative order for the new node.

## Attach mode

`playbooks/add-region.yml` builds a new region against an environment the v1
playbooks built. Rule 9 is the whole contract: the existing hosts are only
partly described by the inventory, so the run adds to them — a `file_sd`
fragment, firewall appends, an `authorized_keys` entry, appended environment
keys — and never re-renders a file on them. Roles that touch a shared host in
attach mode expose a separate entry point for it (`tasks_from: append`,
`file_sd`, `attach_prep`) rather than branching inside `main.yml`. Operator-facing detail is in
[attach-mode.md](attach-mode.md).

## Ownership

Each role's `README.md` states what it owns. Files that more than one role
could plausibly write have exactly one owner:

* the `ops` docker network → `metrics`;
* `/etc/update-motd.d/50-computestacks` → `common`;
* `/etc/modules-load.d/cs-firewall.conf` → `firewall`;
  `/etc/modules-load.d/cs-node.conf` → `node_kernel` (separate files, no
  sharing);
* on nodes, the `docker_data_device` filesystem, its fstab entries (the data
  mount and the `/var/lib/docker` and `/var/lib/containerd` bind mounts) and
  `/etc/systemd/system/{docker,containerd}.service.d/10-cs-data-disk.conf` →
  `node_storage`;
* `/etc/ansible/facts.d/computestacks.fact`, key `tailscale_ip` →
  `tailscale` (merged into the file, never overwritten).

## Interface contracts

### Variable scope

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
| `public_ipv6` | the INVENTORY, as a node host var | optional; one IPv6 host address in canonical RFC 5952 form (roles/preflight); unset, null and empty all mean none | it is a plain host var — `hostvars[h].public_ipv6` is fine |
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

`roles/controller_seed` therefore runs `DRY_RUN=1` on every path that can
reach an existing row — attach mode, or a run with the address flag — and
fails the run when the preview reports drift on a load balancer's `domain`
("gate G" in the role's code and comments). `controller_seed_dry_run_first` governs only whether that preview is
PRINTED; it cannot switch the gate off.

A converged environment re-run without `existing_env` gets neither preview nor
gate. That residue is accepted: production load balancers run `le`, and
`LoadBalancer#deployable_shared_certificate` serves the LetsEncrypt bundle
while it is active, so the rotated `shared_certificate` is a dormant fallback
rather than the certificate tenants receive.

**Operator remediation when gate G fires:** change that load balancer's domain
in the admin UI first, then re-run the seed. Do not edit the inventory to
match the stale row unless the row is what you actually want — the inventory
is also what `roles/controller` names the certificate after.

### Controller invocation helper

The v2 `cstacks` CLI on the controller provides

    cstacks runner '<ruby one-liner>'

which executes `bin/rails runner` inside the running `portal` container,
passes stdout through and propagates the exit code. `cs_agent`'s enroll task
runs, delegated to the controller host,

    cstacks runner "puts Node.find_by!(hostname: %q{<node hostname>}).agent_token_hash"

and writes the 64-hex result into `metadata.admin_token_hash` in
`/etc/computestacks/agent.yml`, then restarts cs-agent. Empty or non-hex
output fails the task loudly.

### agent.yml (owned by `cs_agent`)
- `metadata.listen_addr`: `:8500` when the node routes control-plane traffic
  over the tailnet (§Tailscale access-only mode), else `<primary_ip>:8500`. NEVER the tailscale IP (containers dial
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

### Tailscale access-only mode
`tailscale_access_only: true` (per host or group, default `false`) keeps a
member on the tailnet — the `tailscale` role still joins it, its firewall
still opens the WireGuard port and drops tailnet sources off `tailscale0` —
but for operator access only: no control-plane leg is routed over the
tailnet to or from it. Every pairwise consumer therefore asks a second,
narrower question, the ROUTING predicate, which also has exactly one
spelling:

    ((tailscale_authkey | default('')) | length > 0)
      and (tailscale_enabled | default(true) | bool)
      and not (tailscale_access_only | default(false) | bool)

(`hostvars[h].` in front of each term for another host.) Membership gates
the `tailscale` role, the firewall's anti-spoof rules and its UDP 41641
accept, and preflight's opt-out warning; routing gates everything that picks
an address or a `tailscale0` accept: the manifest's `node.agent_host` and
`acme_server`, prometheus scrape addresses, the firewall's tailnet-path
accepts, the agent's `metadata.listen_addr`, and validate's probes. The
variable has no role default — it must read the same from every host's
`hostvars`, so it is always spelled with `| default(false)`.

### Container network placement

Node-side containers (cadvisor, fluentd) run with `--network=host`, with
listeners bound to `primary_ip` (cadvisor) or `127.0.0.1` (fluentd's
docker-log-driver port): published-port DNAT bypasses the input chain, host
networking does not, so the firewall can enforce them. Metrics-host
containers bind `127.0.0.1` behind nginx.

### Tailscale address derivation (PAIRWISE — never per-node)
"Routes over the tailnet" below is the routing predicate of §Tailscale
access-only mode, asked of both ends.
- Manifest `node.agent_host` = node tailscale IP ONLY if the CONTROLLER and
  the node both route over the tailnet; else omit (controller dials
  primary_ip).
- Prometheus scrape address for a node = tailscale IP ONLY if the METRICS host
  and the node both route over the tailnet; else primary_ip/public route.
- Enrollment/cstacks-runner is controller-local — unaffected.

### Prometheus contract (metrics role + validate)
file_sd labels per node target: `region: <az>`, `node: <hostname>`;
`job_name: node-exporter` (also `cadvisor`, `haproxy` jobs as v1). The
controller's placement queries use exactly
`node="<hostname>",region="<az>",job=~"node-exporter"` — wrong labels means
every node reports 0 cpu/0 mem and ALL orders are rejected. validate asserts a
non-empty query result per node.

### Manifest

`controller_seed` renders the bootstrap manifest; its schema is owned by the
controller repository (`doc/bootstrap_manifest.md` there). Sections may be
omitted. The rendered file holds secrets: root 0600, deleted after a
successful apply. `cstacks seed` wraps `rake bootstrap:apply[<path>]`;
`DRY_RUN=1` is supported and is what attach mode's drift gate reads.

## Deliberate choices

Things that look like inconsistencies and are not:

* **`add-region.yml` runs the whole `docker_tls` role on the new node**, not
  `tasks_from: issue`. `main.yml` routes through `issue.yml` — the piece that
  reaches the existing controller's vault — and then installs the listener
  drop-in, without which the certificate sits on disk and the controller has
  nothing to dial.
* **Two containerization styles.** `metrics`, `loki` and
  `node_observability` wrap `docker run --rm` in hand-written systemd units;
  `vault` uses `community.docker.docker_container`. Both satisfy rule 3. If
  they are ever unified, keep the `docker_container` form, and change all
  three roles in one go rather than as a drive-by.
* **`loki_image` and `fluentd_loki_image` move together**, as do
  `borg_version` and `borg_image`; `versions.yml` marks both pairs.
* **`playbooks/vars/Ubuntu-26.yml`** supplies the platform variables the
  pinned `geerlingguy.postgresql` lacks for Ubuntu 26.04. `include_vars`
  falls back to the playbook directory, so the galaxy role needs no fork.
* **`tests/roles.yml` is one harness for every role** in isolation and every
  reusable `tasks_from:` entry point — the parts the two playbooks' syntax
  checks cannot reach.

## Before you send a change

* `make check` and `make lint` pass (the production ansible-lint profile).
* A new or changed role's `README.md` says what it owns, its variables, and
  why it does what it does.
* Anything an operator would notice gets a dated `CHANGELOG.md` entry.
* Nothing in the diff names a real host, address or credential (rule 6).
* Behavior that only a real host can show — disks, reboots, attach mode — is
  run end to end against throwaway VMs before it merges.
