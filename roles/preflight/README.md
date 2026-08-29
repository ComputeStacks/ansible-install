# preflight

**Owner:** Wave 1A

Assert-only role. It never changes a host — it only fails loudly (or, for the
tailscale case, warns) before any other role runs. Runs on `hosts: all` with
`gather_facts: true` (see `playbooks/site.yml`).

## What it asserts

- Every host is Ubuntu 26.04 amd64/x86_64 — **skipped** on hosts flagged
  `existing_env: true` (pre-existing v1 environments are Debian).
- Every node in `groups['nodes']` defines: `hostname`, `primary_ip`,
  `public_ip`, `region`, `az`, `container_network`, `container_network_name`.
- `az` values are unique across `groups['nodes']` (exactly one node per
  availability zone).
- Every host's `hostname` (where defined), including nodes and shared hosts,
  is a single lowercase word — letters, digits, hyphens only, no dots, no
  uppercase (not an FQDN).
- `secret_key_base` and `user_auth_secret` are defined, at least 128
  characters, and not the `CHANGEME` sample placeholders. **This role never
  generates these values** — they are immutable install inputs
  (docs/contracts.md rule 8); rotating them later destroys every encrypted
  credential in the controller.
- If `tailscale_authkey` is set, hosts with `tailscale_enabled: false` are
  reported via a `debug` warning (not a failure) — same-L2 regions
  legitimately opt out, but the operator should see the list and confirm it's
  intentional.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `preflight_required_node_vars` | see `defaults/main.yml` | List of host vars every node must define. |

Consumed from elsewhere in the inventory (not owned by this role):
`existing_env`, `secret_key_base`, `user_auth_secret`, `tailscale_authkey`,
`tailscale_enabled`.

## Failure messages

Every assertion's `fail_msg` names the exact host and variable (or value)
that needs fixing, and where to fix it — no generic "preflight failed"
messages.
