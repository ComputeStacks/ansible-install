# v2 implementation contracts (frozen — Wave 0)

Binding for every implementer. The full plan lives outside the repo
(workspace doc); this file is the subset an implementer must not violate.
Change requests go to the engineering manager, not into the code.

## Vocabulary
`region` == controller `Location` (e.g. ams005). `az` == controller `Region`
(e.g. ams-005). Exactly ONE node per az — enforced by preflight, relied on
everywhere. The Location/Region mapping exists ONLY inside the seeding layer.

## Hard rules
1. **Inventory vars only in shared-host and seed templates.** Never gathered
   facts (`ansible_hostname`, `ansible_default_ipv4`, …) — under `--limit`,
   un-targeted hosts have no facts and their regions silently vanish from
   rendered config. Required node vars: `hostname`, `primary_ip`, `public_ip`,
   `region`, `az`, `container_network`, `container_network_name`.
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
(both defaults mandatory). The tailscale role owns the `tailscale_ip` key in
`/etc/ansible/facts.d/computestacks.fact` and MERGES into that file, never
overwrites. Tailnet MEMBERSHIP remains a pure-inventory predicate
(`tailscale_authkey` set and `tailscale_enabled | default(true)`) — only the
address VALUE uses this exception.

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
6. **Two pins are deliberately empty**, with VERIFY notes in `versions.yml`:
   the docker engine packages and `node_exporter_apt_version`. Neither
   upstream publishes anything for Ubuntu 26.04 yet, so there is no version
   string to pin honestly; both packages are `apt-mark hold`-ed after
   install, which is what v1 actually relied on, and the holds are released
   before each install so a later pin still rolls through.
7. **`playbooks/vars/Ubuntu-26.yml`** supplies the platform variables
   `geerlingguy.postgresql` 4.0.0 is missing for Ubuntu 26 — its first task
   is an `include_vars` that would otherwise fail outright. `include_vars`
   falls back to the playbook directory, so this needs no fork of the pinned
   role.
8. **The eight per-wave syntax harnesses are one file**, `tests/roles.yml`.
   They existed because `site.yml` did not parse yet. It does now, so the
   harness keeps only what the two playbooks cannot cover: every role in
   isolation and every reusable `tasks_from:` entry point.

## Definition of done (every provisioner wave)
`ansible-lint roles/<role>` clean (new .ansible-lint at repo root, production
profile); `ansible-playbook --syntax-check` of a minimal per-role test play if
site.yml doesn't parse yet; role README.md (purpose, vars, owner); no writes
outside your allowlist; STOP AND REPORT rather than guess on any ambiguity —
that is a successful outcome, silent guessing is not.
