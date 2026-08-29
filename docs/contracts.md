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

### Definition of done (every provisioner wave)
`ansible-lint roles/<role>` clean (new .ansible-lint at repo root, production
profile); `ansible-playbook --syntax-check` of a minimal per-role test play if
site.yml doesn't parse yet; role README.md (purpose, vars, owner); no writes
outside your allowlist; STOP AND REPORT rather than guess on any ambiguity —
that is a successful outcome, silent guessing is not.
