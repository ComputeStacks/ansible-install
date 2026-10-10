# vault

Runs the `vault-bootstrap` container on the controller and the `pki/docker` CA
that signs the controller's docker client certificate and every node's docker
daemon certificate; its `unseal` and `ca_ttl` entry points are reused from
nodes and on their own.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Controller infrastructure" | `controller` |
| `unseal.yml` (`tasks_from: unseal`) | "Unseal the controller vault" | `controller` |
| `roles/docker_tls` (`tasks_from: unseal`, `ca_ttl`) | `site.yml` "Node docker TLS"; `add-region.yml` "New node docker TLS, issued from the existing Vault" | `nodes`, delegated to `vault_host` |
| `add-region.yml` (whole role), `upgrade-docker.yml` | not run | — |

**Attach mode:** the role itself never runs against an `existing_env`
controller; only `unseal` and `ca_ttl`, through `docker_tls` on the new node,
reach the existing controller's vault (unseal if sealed, read the CA).
**Requires:** `geerlingguy.docker` and `docker_config` on the controller
(the "Docker hosts" play).

## Purpose

Runs the `vault-bootstrap` container on the controller and owns the
`pki/docker` secrets engine — the CA that signs

* the controller's docker **client** certificate (mounted into the portal
  container as `/root/.docker`), and
* every node's docker daemon **server** certificate (issued by `docker_tls`,
  which reuses this role's task files).

This PKI is interim. cs-agent Phase 4 (controller-as-CA) replaces it, which is
why leaf TTLs are short (1 year) and why nothing here tries to be a
long-lived CA management system.

## What it does, in order

1. Asserts `vault_image` is an exact pin (from `group_vars/all/versions.yml`).
2. Creates the on-disk layout under `/etc/computestacks/.vault-bootstrap/` —
   the same layout v1 used, so attach mode finds an existing controller's
   keys where it expects them.
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
   `vault_cert_renew_days` of expiry. `roles/controller` records the
   certificate's fingerprint and recreates the portal container when it
   changes — the portal reads the certificate only at boot.

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
— that is how `docker_tls`, in `site.yml` and in attach mode
(`add-region.yml`), reaches the controller's vault, including an **existing**
controller's. `unseal.yml` never creates or recreates the
container; on an existing controller it will only `docker start` a stopped one.

`playbooks/unseal.yml` is the same include on its own, for when a converge is
not what you want:

```
ansible-playbook -i inventories/<env> playbooks/unseal.yml --ask-vault-pass
```

A rebooted controller comes back sealed, and nothing surfaces that until
something asks vault for a certificate — the docker daemon certs already on
disk are static files and keep working. Any normal converge unseals on its
way past; that playbook is the shortcut.

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
| `vault_container_uid` / `vault_container_gid` | `100` / `1000` | The `vault` user inside `vault_image`. `file` and `logs` are created with this ownership, not root's: the image entrypoint chowns them to `vault` at container START and then drops privileges, so a re-converge that reset them to root would lock the running server out of its own storage. Re-check if `vault_image` moves. |
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

## Notes on the contracts

* `vault_listen_port: 8200` is a role default rather than a `cs_ports` entry.
  `ports.yml` is the *cross-host* port contract (firewall, scrape configs,
  vhosts, manifest); vault's listener is loopback-only, never firewalled, and
  referenced by no other role.
* `groups['controller'][0]` is used only as a **delegation target**, which
  `docs/contracts.md` rule 2 permits.
* `client.pem` (cert+ca+key concatenation) is also written, matching the
  layout an existing controller has; no consumer for it is known in this repo.
