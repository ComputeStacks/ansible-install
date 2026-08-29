# docker_tls

**Owner: Wave 1B.** Nodes only.

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
   `ExecStart` with the TCP + `--tlsverify` listener plus v1's node hardening
   flags, and restarts docker **via a handler** when anything changed.

`daemon.json` is **not** touched here — `roles/docker_config` owns it on all
docker hosts. Docker hard-errors when an option is set both there and on the
command line, so the ExecStart line deliberately carries no
`--live-restore`/`--registry-mirror`.

## Convergence (the deliberate difference from v1)

v1 issued a certificate exactly once (`when: not key_exists`) and never looked
at it again — a 5-year cert nobody watched. v2 re-issues when, and only when:

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
the CA's remaining lifetime (v1 asked for 5 years, unclamped).

## Attach mode (`existing_env: true`)

`tasks/issue.yml` is self-contained for exactly this reason:

```yaml
- name: Issue the node docker daemon certificate
  ansible.builtin.include_role:
    name: docker_tls
    tasks_from: issue
```

It unseals the existing controller's vault first (a v1 controller that has ever
rebooted is sealed, and nobody notices because certs are static files), reads
the unseal keys from v1's own layout, and writes **nothing** on the controller.
No new infrastructure, no re-render of anything on a shared host.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `docker_tls_listen_ip` | `{{ primary_ip }}` | Inventory var, never a gathered fact. v1 bound `0.0.0.0`; v2 binds the private address. |
| `docker_tls_common_name` | `{{ hostname }}` | |
| `docker_tls_alt_names` | `[localhost, {{ hostname }}]` | DNS SANs. |
| `docker_tls_ip_sans` | `[127.0.0.1, {{ primary_ip }}]` | |
| `docker_tls_renew_days` | `30` | Re-issue window. |
| `docker_tls_daemon_flags` | `[--icc=false, --userland-proxy=false]` | v1's node hardening, unchanged. `--userland-proxy=false` is why `node_kernel` raises the conntrack limits. |
| `docker_tls_certs_dir` | `/etc/docker/certs` | v1 path. |
| `docker_tls_ca_file` / `docker_tls_cert_file` / `docker_tls_key_file` | `ca.crt` / `server.crt` / `server.key` | v1 names. |
| `docker_tls_dropin_dir` / `docker_tls_dropin_file` | `/etc/systemd/system/docker.service.d` / `startup.conf` | v1 filename kept so a host can never carry two ExecStart drop-ins. |
| `docker_tls_bin` | `/usr/bin/dockerd` | |
| `docker_tls_create_dockremap_user` | `true` | See below. |
| `docker_tls_dockremap_uid` | `1001` | |

Consumed from elsewhere: `cs_ports.docker_tls` (ports.yml), `hostname` and
`primary_ip` (inventory, asserted), and the `vault` role's `vault_command`,
`vault_host`, `vault_pki_docker_path`, `vault_effective_cert_ttl_hours`.

## Requirements

* The `vault` role must be present (its task files are included) and the
  controller's `vault-bootstrap` container must exist.
* `community.crypto` + `community.docker` (pinned in `requirements.yml`);
  `cryptography` on the ansible control machine.
* Run after `geerlingguy.docker` and `docker_config`.

## Deviations from v1

* Convergent re-issue (expiry / SAN change) instead of issue-once-forever.
* TTL 1 year, clamped to the CA (v1: 43800h, unclamped).
* Binds `primary_ip:2376`, not `0.0.0.0:2376`.
* SANs come from the inventory (`hostname`, `primary_ip`); v1 used
  `ansible_hostname` (a gathered fact — banned by contracts rule 1) and a
  `consul_listen_ip` var that defaulted to `127.0.0.1`, which quietly produced
  certificates with no usable IP SAN unless the inventory overrode it.
* Restart via handler (v1 stopped docker, wrote the drop-in, then started it —
  on every single run).
* File modes: dir 0750, `server.crt`/`ca.crt` 0640, `server.key` 0600 (v1: dir
  0740, everything 0440).
* `--registry-mirror` moved out of ExecStart into `daemon.json`
  (`docker_config_registry_mirrors`).
* `docker exec` without `-it`, so v1's `ansible_ssh_pipelining: no` workaround
  is gone.

### dockremap

v1 created a `dockremap` user (uid 1001) but never enabled `userns-remap` — not
in its ExecStart, not in `daemon.json`. It is **vestigial**; it is ported for
parity only and can be switched off with
`docker_tls_create_dockremap_user: false`. If user-namespace remapping is ever
actually wanted, docker creates and manages the user itself when
`userns-remap` is set, and enabling it would break every existing container's
ownership — so this should be deleted rather than "finished".
