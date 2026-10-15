# docker_tls

Gives each node's dockerd its mTLS TCP listener on `primary_ip:2376`, with a
server certificate issued by the controller's vault.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Node docker TLS" | `nodes` |
| `site.yml` (`tasks_from: clear_stale_dropin`) | "Docker hosts" (pre_task) | `nodes` only |
| `add-region.yml` | "New node docker TLS, issued from the existing Vault" | `nodes` (the new node) |
| `add-region.yml` (`tasks_from: clear_stale_dropin`) | "New node docker" (pre_task) | `nodes` (the new node) |
| `maintain-nodes.yml` (via `node_upgrade` `tasks_from: maintain`) | "Maintain the nodes, one at a time" | `maintain_node_targets` (live nodes, inside a maintenance hold; restart deferred) |
| `maintain-nodes.yml` (`tasks_from: restart_files`, via `node_upgrade`) | "Maintain the nodes, one at a time" | `maintain_node_targets` |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** new node only; issuance reaches the existing controller's
vault but writes nothing there (see "Attach mode"). **Requires:** the `vault`
role's task files and the controller's `vault-bootstrap` container;
`geerlingguy.docker` + `docker_config` in an earlier play.

## Purpose

Gives a node's docker daemon its mTLS listener on
`{{ primary_ip }}:{{ cs_ports.docker_tls }}` (2376), with a server certificate
issued by the controller's vault (`pki/docker/roles/server`). This is
control-plane edge #2: the controller dials every node's dockerd on
`primary_ip:2376` with the client certificate the `vault` role writes.

It does three things:

1. **Issues/renews the daemon certificate** (`tasks/issue.yml`) into
   `/etc/docker/certs/{server.crt,ca.crt,server.key}`.
2. Creates the `dockremap` user (v1 parity — see below).
3. Deploys `/etc/systemd/system/docker.service.d/startup.conf`, which replaces
   `ExecStart` with the TCP + `--tlsverify` listener plus the node hardening
   flags (`docker_tls_daemon_flags`), and restarts docker **via a handler** when anything changed.

`daemon.json` is **not** touched here — `roles/docker_config` owns it on all
docker hosts. Docker hard-errors when an option is set both there and on the
command line, so the ExecStart line deliberately carries no
`--live-restore`/`--registry-mirror`.

## Convergence

The certificate is re-issued when, and only when:

* the certificate file is missing, **or**
* it is within `docker_tls_renew_days` (30) of `notAfter`, **or**
* its SAN set no longer matches the inventory (`hostname` / `primary_ip`
  changed).

A healthy certificate is never rotated. The check reads the on-disk cert with
`community.crypto.x509_certificate_info`, executed on the ansible control
machine (`delegate_to: localhost`) so nodes need no python crypto libraries.

Issuance itself: `include_role: vault, tasks_from: unseal` → `tasks_from:
ca_ttl` → `docker exec vault-bootstrap vault write -format=json
pki/docker/issue/server …`, delegated to `vault_host`
(`groups['controller'][0]`, a delegation target — permitted by contracts rule
2). The requested TTL is `vault_effective_cert_ttl_hours`: 1 year, clamped to
the CA's remaining lifetime.

## Deferred restart (`maintain-nodes.yml`)

`maintain-nodes.yml` converges this role on a LIVE node, inside a cs-agent
maintenance hold, with the tenant containers running. It sets the play var
`docker_tls_restart_deferred: true`, and then:

* the "Restart docker" handler does not run;
* "Record a deferred docker restart" (same notification, `listen`) adds a
  reason to `docker_tls_restart_deferred_reasons` instead;
* `roles/node_upgrade` reads that list after flushing the handlers inside
  the hold, together with an independent check -- any file in
  `tasks/restart_files.yml`'s list (the certificate, CA, key and the
  drop-in) modified after dockerd last started -- and, if either says so,
  stops the recorded containers and restarts dockerd in that window.

The file check covers a run that died after writing a certificate but before
recording the reason. Everywhere else the variable is false and the handler
restarts dockerd exactly as before. A new handler in any role the maintain
converge includes that restarts dockerd must carry the same gate;
`tests/maintain_nodes.yml` fails otherwise.

## Check mode

`--check` does not issue: the issuance reads (and may unseal) the controller's
vault and writes a key pair. When a certificate would be issued or renewed,
the run says so and notifies "Restart docker", so `maintain-nodes.yml`'s
preview reports "would renew -> docker restart required". The probes (stat,
slurp, `x509_certificate_info`) all run in check mode.

## Attach mode (`existing_env: true`)

`playbooks/add-region.yml` runs the **whole role** on the new node, not just
`tasks_from: issue`: `tasks/main.yml` routes through `tasks/issue.yml`, which
is the part that reaches the existing controller, and the rest of `main.yml` —
the listener drop-in on `primary_ip:2376` — is what makes the new node
dialable at all.

`tasks/issue.yml` unseals the existing controller's vault first (a v1
controller that has ever rebooted is sealed, and nobody notices because certs
are static files), reads the unseal keys from v1's own layout, and writes
**nothing** on the controller. No new infrastructure, no re-render of anything
on a shared host. It is self-contained, so it can also be included on its own
with `tasks_from: issue`.

## Stale drop-in cleanup (`tasks/clear_stale_dropin.yml`)

Included as a pre_task by the docker plays in `site.yml` and `add-region.yml`,
**before** `geerlingguy.docker` starts the daemon. The drop-in this role wrote
on a previous converge pins `-H tcp://<primary_ip>:2376`; if the host's
`primary_ip` has changed since, dockerd refuses to start (`bind: cannot assign
requested address`) and the run dies several plays before this role's main
task could rewrite the file. So a drop-in naming an endpoint the host can no
longer bind is removed and systemd reloaded, docker starts on its default
socket, and `tasks/main.yml` writes the correct drop-in later in the same run.
Only this role's own `startup.conf` is ever removed, and only on `nodes`.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `docker_tls_listen_ip` | `{{ primary_ip }}` | Inventory var, never a gathered fact. The private address, never `0.0.0.0`. |
| `docker_tls_common_name` | `{{ hostname }}` | |
| `docker_tls_alt_names` | `[localhost, {{ hostname }}]` | DNS SANs. |
| `docker_tls_ip_sans` | `[127.0.0.1, {{ primary_ip }}]` | |
| `docker_tls_renew_days` | `30` | Re-issue window. |
| `docker_tls_daemon_flags` | `[--icc=false, --userland-proxy=false]` | Node hardening (the same flags v1 used). `--userland-proxy=false` is why `node_kernel` raises the conntrack limits. |
| `docker_tls_certs_dir` | `/etc/docker/certs` | v1 path. |
| `docker_tls_ca_file` / `docker_tls_cert_file` / `docker_tls_key_file` | `ca.crt` / `server.crt` / `server.key` | v1 names. |
| `docker_tls_dropin_dir` / `docker_tls_dropin_file` | `/etc/systemd/system/docker.service.d` / `startup.conf` | v1 filename kept so a host can never carry two ExecStart drop-ins. |
| `docker_tls_bin` | `/usr/bin/dockerd` | |
| `docker_tls_create_dockremap_user` | `true` | See below. |
| `docker_tls_dockremap_uid` | `1001` | |
| `docker_tls_restart_deferred` | `false` | Record "docker restart required" instead of restarting dockerd. Set by `maintain-nodes.yml` only. |

Consumed from elsewhere: `cs_ports.docker_tls` (ports.yml), `hostname` and
`primary_ip` (inventory, asserted), and the `vault` role's `vault_command`,
`vault_host`, `vault_pki_docker_path`, `vault_effective_cert_ttl_hours`.

## Requirements

* The `vault` role must be present (its task files are included) and the
  controller's `vault-bootstrap` container must exist.
* `community.crypto` + `community.docker` (pinned in `requirements.yml`);
  `cryptography` on the ansible control machine.
* Run after `geerlingguy.docker` and `docker_config`.

## dockremap

v1 created a `dockremap` user (uid 1001) but never enabled `userns-remap` — not
in its ExecStart, not in `daemon.json`. It is **vestigial**; it is kept for
parity only and can be switched off with
`docker_tls_create_dockremap_user: false`. If user-namespace remapping is ever
actually wanted, docker creates and manages the user itself when
`userns-remap` is set, and enabling it would break every existing container's
ownership — so this should be deleted rather than "finished".
