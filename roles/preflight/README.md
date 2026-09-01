# preflight

**Owner:** Wave 1A

Assert-only role. It never changes a host — it only fails loudly (or, for the
tailscale case, warns) before any other role runs. Runs on `hosts: all` with
`gather_facts: true` (see `playbooks/site.yml`). The registry checks below read
from the network, which is still not a change to a host.

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
- Every registry in `docker_registries` accepts its credentials, checked from
  each docker host (`preflight_docker_groups`).
- The controller image named by `controller_image_repo:controller_image_tag`
  exists and is pullable with whatever credentials apply — run on controller
  hosts, public repository or not, because a typo'd tag is as fatal as a bad
  credential.

## The registry checks (`tasks/registry.yml`, `tasks/registry_probe.yml`)

These speak the registry HTTP API rather than running `docker pull`, because
preflight runs before `geerlingguy.docker` and a greenfield host has no docker
daemon yet. The probe is the standard OCI token handshake: `GET /v2/` answers
`401` with a `WWW-Authenticate: Bearer realm=…,service=…` challenge, the realm
mints a token from the credentials, and the token reads the manifest. A
registry that answers `200` anonymously skips the token leg.

Two special cases are handled: Docker Hub's API host is `registry-1.docker.io`
rather than the `docker.io` written in an image reference, and a single-segment
Hub repository is really `library/<name>`.

Credentials-only probes ask for an unscoped token — every supported registry
mints one for valid credentials and refuses for invalid ones, which is the
whole question. The controller probe additionally asks for
`repository:<path>:pull` and then reads the manifest, so it separates "your
credentials are wrong" from "that tag does not exist".

`url_password` is `no_log` in ansible's own argument spec, so the password is
censored even at `-vvv` without hiding the rest of the diagnostics — which is
exactly why credential verification lives here and not in
`roles/docker_config`, where the login task must be `no_log` wholesale.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `preflight_required_node_vars` | see `defaults/main.yml` | List of host vars every node must define. |
| `preflight_docker_groups` | `controller`, `metrics`, `registry`, `nodes` | Groups whose hosts run a docker daemon. Mirrors the `hosts:` line of site.yml's "Docker hosts" play; a host outside them never logs in to a registry. |
| `preflight_verify_controller_image` | `true` | Set false for an air-gapped converge, or where egress goes through something the control path cannot see. |
| `preflight_registry_manifest_accept` | OCI + docker, index + manifest | `Accept` header for the manifest read. Which type comes back depends on how the image was built and pushed. |

Consumed from elsewhere in the inventory (not owned by this role):
`existing_env`, `secret_key_base`, `user_auth_secret`, `tailscale_authkey`,
`tailscale_enabled`, `cs_registry_logins` / `cs_controller_registry` (composed
in `playbooks/group_vars/all/registries.yml`), `controller_image_repo`,
`controller_image_tag`.

## Failure messages

Every assertion's `fail_msg` names the exact host and variable (or value)
that needs fixing, and where to fix it — no generic "preflight failed"
messages.
