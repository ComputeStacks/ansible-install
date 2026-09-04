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
  each docker host (`preflight_docker_groups`) that is not flagged
  `existing_env` — in attach mode only the new nodes ever log in.
- Every entry in `docker_registries` is a complete `{registry, username,
  password}` triple and no registry host is named twice.
- `controller_image_tag` names a fixed build, not a floating tag
  (`latest`/`stable`/`main`/`master`), unless
  `controller_image_allow_floating_tag` is true. A local check, so it runs even
  with `preflight_verify_registries: false`, and it runs here rather than only
  in `roles/controller` so an unintended `latest` costs seconds instead of most
  of a converge.
- The controller image named by `controller_image_repo:controller_image_tag`
  exists and is pullable with whatever credentials apply — on controller hosts
  that are not `existing_env`, public repository or not, because a typo'd tag
  is as fatal as a bad credential.

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

A credentials-only probe asks for an *unscoped* token and reads the
distinction between `401` and `403` rather than insisting on a `200`: a
self-hosted GitLab answers an unscoped request with `403` even for good
credentials, while wrong credentials get `401` from gitlab.com and self-hosted
alike. So `401` means "not valid" and `403` means "valid, but this request
granted no scope" — which is all a probe that does not yet know a repository
can honestly claim. The controller probe instead asks for
`repository:<path>:pull` and reads the manifest, so it separates "your
credentials are wrong" from "that tag does not exist".

Both requests that carry or return a credential are `no_log` — the password
rides in one, and the *token the registry mints* is itself a working pull
credential that ansible would otherwise print from `-v` upward. Neither request
decides anything: each is followed by an assert that reads only the status code
(and, on a transport failure, the module's own message) and writes the
diagnosis itself, and a registered result survives `no_log` intact. That is why
credential verification lives here rather than in `roles/docker_config`, whose
login task has to be `no_log` as a whole and so fails mutely.

Every request also carries `check_mode: false`. `uri` does not support check
mode, so under `--check` the probes would skip, every downstream assert would
skip with them, and a bad credential would report green.

One case cannot be verified: a registry that answers `GET /v2/` anonymously
issues no challenge, so there is no endpoint to test the credentials against.
The probe says so in a `debug` warning rather than reporting a success it did
not earn.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `preflight_required_node_vars` | see `defaults/main.yml` | List of host vars every node must define. |
| `preflight_verify_registries` | `true` | Every network read in this role, in one switch. False makes preflight purely local, for an air-gapped converge. |
| `preflight_docker_groups` | `controller`, `metrics`, `registry`, `nodes` | Groups whose hosts run a docker daemon. Mirrors the `hosts:` line of site.yml's "Docker hosts" play; a host outside them never logs in to a registry. |
| `preflight_verify_controller_image` | `true` | The image existence-and-tag check specifically; the credential probes stay on. |
| `preflight_registry_manifest_hint_404` / `_denied` | see `defaults/main.yml` | The two ways a manifest read fails, as operator-facing sentences. |
| `preflight_registry_credentials_hint` | see `defaults/main.yml` | What a `401` from the token endpoint means, as an operator-facing sentence. |
| `preflight_image_pin_hint` | see `defaults/main.yml` | Why a floating controller tag is refused, and how to override, as an operator-facing sentence. |
| `preflight_registry_manifest_accept` | OCI + docker, index + manifest | `Accept` header for the manifest read. Which type comes back depends on how the image was built and pushed. |

Consumed from elsewhere in the inventory (not owned by this role):
`existing_env`, `secret_key_base`, `user_auth_secret`, `tailscale_authkey`,
`tailscale_enabled`, `cs_registry_logins` / `cs_controller_registry` (composed
in `playbooks/group_vars/all/registries.yml`), `controller_image_repo`,
`controller_image_tag`, `controller_image_allow_floating_tag`.

## Failure messages

Every assertion's `fail_msg` names the exact host and variable (or value)
that needs fixing, and where to fix it — no generic "preflight failed"
messages.
