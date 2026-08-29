# vault

**Owner: Wave 1B.** Slim port of v1's `vault` role.

## Purpose

Runs the `vault-bootstrap` container on the controller and owns the
`pki/docker` secrets engine — the CA that signs

* the controller's docker **client** certificate (mounted into the portal
  container as `/root/.docker`), and
* every node's docker daemon **server** certificate (issued by `docker_tls`,
  which reuses this role's task files).

v1's `pki/consul` mount is **not** ported — consul is gone from the stack.

This PKI is interim. cs-agent Phase 4 (controller-as-CA) replaces it, which is
why leaf TTLs are short (1 year, not v1's 5) and why nothing here tries to be a
long-lived CA management system.

## What it does, in order

1. Asserts `vault_image` is an exact pin (from `group_vars/all/versions.yml`).
2. Creates the v1 on-disk layout under `/etc/computestacks/.vault-bootstrap/`.
3. **First converge only** (`keys/unseal_key_0` absent): starts the container
   without the root-token bind mount, runs `operator init -key-shares=5
   -key-threshold=3`, and writes `keys/unseal_key_{0..4}` + `keys/rootkey`
   (mode 0600).
4. Converges the container to the pinned image, with `keys/rootkey` mounted at
   `/root/.vault-token`. An image bump in versions.yml recreates it here.
5. **Unseal-if-sealed** (`tasks/unseal.yml`).
6. Enables + tunes `pki/docker` and generates the root CA if the mount is
   absent; (re)writes the `server` and `client` PKI roles every run, so a TTL
   change in defaults rolls out.
7. Issues the controller client cert if it is missing or within
   `vault_cert_renew_days` of expiry.

Re-running does not re-init, does not recreate the CA, and does not rotate a
healthy certificate.

## Reusable task files (`tasks_from:`)

```yaml
- name: Ensure the controller vault is unsealed
  ansible.builtin.include_role:
    name: vault
    tasks_from: unseal        # idempotent; no-op when already unsealed

- name: Compute the clamped certificate TTL
  ansible.builtin.include_role:
    name: vault
    tasks_from: ca_ttl        # sets vault_effective_cert_ttl_hours
```

Both delegate every action to `vault_host`, so they work unchanged from a node
— that is how `docker_tls` and attach mode (`add-region.yml`) reach an
**existing** v1 controller's vault. `unseal.yml` never creates or recreates the
container; on an existing controller it will only `docker start` a stopped one.

### The TTL clamp

Vault refuses — with an unhelpful error — to sign a leaf whose `notAfter` runs
past the CA's. `ca_ttl.yml` reads the CA, computes its remaining lifetime, and
sets

```
vault_effective_cert_ttl_hours = min(vault_cert_ttl_hours,
                                     CA remaining hours - vault_cert_ttl_clamp_margin_hours)
```

failing loudly if that is ≤ 0. Every issuance in this repo passes that value as
`ttl=<n>h`.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `vault_image` | **required** | Exact pin; lives in `group_vars/all/versions.yml`. The role asserts it is set and not `:latest`. |
| `vault_host` | `groups['controller'][0]` | Delegation target for every vault action. |
| `vault_container_name` | `vault-bootstrap` | |
| `vault_command` | `docker exec vault-bootstrap vault` | |
| `vault_storage_path` | `/etc/computestacks/.vault-bootstrap` | **v1 layout — do not change**, attach mode reads an existing controller's keys from here. |
| `vault_certificates_path` | `/etc/computestacks/certificates` | |
| `vault_docker_certs_path` | `…/certificates/docker` | Bind-mounted into portal as `/root/.docker`. |
| `vault_listen_address` / `vault_listen_port` | `127.0.0.1` / `8200` | Loopback only. |
| `vault_key_shares` / `vault_key_threshold` | `5` / `3` | v1 parity. |
| `vault_pki_docker_path` | `pki/docker` | |
| `vault_pki_ca_common_name` / `vault_pki_ca_ttl_hours` | `Docker Root CA` / `87600` | |
| `vault_max_lease_ttl_hours` | `87600` | Mount + PKI role `max_ttl`. |
| `vault_cert_ttl_hours` | `8760` (1y) | Requested leaf TTL, then clamped. |
| `vault_cert_ttl_clamp_margin_hours` | `24` | Safety margin under the CA's `notAfter`. |
| `vault_cert_renew_days` | `30` | Re-issue window for the controller client cert. |
| `vault_client_common_name` | `portal` | |
| `vault_manage_root_alias` | `true` | `alias vault=…` in `/root/.bashrc`. |

## Requirements

* `community.docker` and `community.crypto` (pinned in `requirements.yml`).
* `community.crypto.x509_certificate_info` runs **on the ansible control
  machine** (`delegate_to: localhost`) against slurped/piped PEM, so the
  managed hosts need no python crypto libraries.

## Deviations from v1

* No `pki/consul` mount, no consul certificates.
* Leaf TTL 8760h (1y) instead of 43800h (5y), and clamped to the CA.
* Unseals whenever vault reports sealed, on every converge (v1 only unsealed on
  the run that happened to see a non-zero `vault status`, and its issuance
  tasks would then fail opaquely).
* Re-issues the controller client cert inside the renewal window. **The portal
  container may need a restart to pick up a renewed client cert** — Wave 3G
  (controller role) should notify its restart handler on
  `{{ vault_docker_certs_path }}/cert.pem` changing.
* `docker exec` without `-it` (no TTY needed), so v1's
  `ansible_ssh_pipelining: no` workaround is gone.
* Key files are mode 0600 (v1 left them at the copy default).
* `client.pem` (cert+ca+key concatenation) is still written for v1 parity; no
  consumer for it is known in this repo.

## Notes on the contracts

* `vault_listen_port: 8200` is a role default rather than a `cs_ports` entry.
  `ports.yml` is the *cross-host* port contract (firewall, scrape configs,
  vhosts, manifest); vault's listener is loopback-only, never firewalled, and
  referenced by no other role. Flagged for the manager: move it into `cs_ports`
  if the contract is meant to cover every port literal.
* `groups['controller'][0]` is used only as a **delegation target**, which
  `docs/contracts.md` rule 2 permits.
