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
- Exactly one host in `groups['controller']`, and — when
  `dns_driver == 'powerdns'` — exactly one in `groups['ns_primary']`.
- **Site scoping** (docs/contracts.md §Vocabulary), three assertions:
  - Every metrics host and backup host declares a `site` host var, checked
    **only when the inventory holds more than one site**. A single-site
    inventory sets `site` nowhere, every host falls into the site called
    `default`, and this check does not fire — no existing inventory needs an
    edit.
  - No site holds more than one metrics host, and every site holding a node
    holds one. A node whose site has no metrics host fails the run: the
    controller's placement queries go to that site's prometheus, and with
    none, every order for its regions is rejected. Nodes are not checked for
    `site` directly — a node that omits it falls into `default` and is named
    by this assertion, which is the more useful message.
  - No site holds more than one backup host. **Zero is legal** and means that
    site's nodes install without backups (`cs_agent` renders
    `backups.enabled: false`); two in one site is the error, because every
    node in a site shares one repository host.

  Together these reduce to the "exactly one metrics host / at most one backup
  host" they replaced whenever the inventory has a single site.
- **Per-az application domains** (`playbooks/group_vars/all/app_domains.yml`),
  five assertions. `app_domain` is an optional inventory HOST var naming the
  domain that az's load balancer answers on; a node that omits it takes the
  environment-wide `cs_app_zone`, which is what every inventory written before
  per-az domains did, so no existing inventory needs an edit.
  - Every `app_domain` that IS set is a bare lowercase domain — no `*`, no
    scheme, no port, no trailing dot, and **not empty**. Empty is the
    dangerous one: it is not "unset", it renders a blank load balancer
    domain, and the controller's `ValidateDomainWorker` returns early on a
    blank domain rather than complaining.
  - No two azs are given the same `app_domain`. Checked only over the nodes
    that **explicitly set** it — the derived `cs_app_domain` is `cs_app_zone`
    for everyone who does not, so an unscoped check would fail every
    inventory in this repository. **It can only see the azs in THIS
    inventory**, and in attach mode the inventory is a partial view of the
    fleet (docs/contracts.md rule 9): a new region whose domain collides with
    an EXISTING production az is not detectable here, and the admin UI is the
    only place to check.
  - Every `app_domain` lies at or under `cs_app_zone`, and `cs_app_zone` is
    2–5 labels. The controller resolves a container's DNS zone by walking up
    the last 2, 3, 4 and 5 labels of its FQDN (`LetsEncryptAuth#dns_zone`) and
    taking the first exact `Dns::Zone` match, so anything outside that finds
    no zone, gets no DNS-01 challenge, and the tenant wildcard certificate
    silently never issues — with nothing failing at install time. A **hard**
    assert for that reason. The suffix test has a label boundary in it:
    with a zone of `usercontent.example.com`, `notusercontent.example.com`
    ends with the string and is not under the zone.
  - **In attach mode** (decided by the CONTROLLER's `existing_env`, the same
    predicate `roles/ssh_trust` uses), every new node names its own
    `app_domain` and it is not `cs_app_zone`. Merely being defined is not
    enough: set equal to the zone it selects the legacy single-certificate
    path, generates nothing, and seeds the new region's load balancer with
    the existing environment's certificate.
  - `controller_wildcard_domain` is **retired** and fails the run if set to
    anything other than `cs_app_zone`. The load balancer domain now comes
    from `app_domain` per az, and that override would move the certificate CN
    while leaving every rendered domain alone.
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
| `preflight_site_hint` | see `defaults/main.yml` | What a `site` is and where to set it, as an operator-facing sentence. Shared by all three site assertions. |
| `preflight_node_sites` | derived | Distinct `cs_site` values across `groups['nodes']`. |
| `preflight_metrics_sites` / `preflight_backup_sites` | derived | The `cs_site` of each metrics / backup host, **not** deduped — comparing against its own `unique` is how "more than one per site" is detected. |
| `preflight_inventory_sites` | derived | Distinct sites across nodes, metrics and backup hosts. `> 1` is what makes `site` a required var. |
| `preflight_sites_without_metrics` | derived | Sites holding at least one node but no metrics host — the fatal case. |
| `preflight_app_domain_hint` | see `defaults/main.yml` | What `app_domain` is, where to set it and what DNS it needs, as an operator-facing sentence. Shared by all five app-domain assertions. |
| `preflight_app_domain_hosts` | derived | Every host that **explicitly sets** `app_domain`, for the shape and zone checks. |
| `preflight_app_domain_nodes` / `preflight_node_app_domains` | derived | The nodes that explicitly set it, and their values — **not** deduped, since comparing against its own `unique` is how the collision is detected. |
| `preflight_app_domains_outside_zone` | derived | Explicitly-set domains that are neither `cs_app_zone` nor beneath it at a label boundary. |
| `preflight_app_zone_labels` | derived | Label count of `cs_app_zone`; the `LetsEncryptAuth` walk reaches 2–5 and nothing deeper. |
| `preflight_attach_mode` | derived | The CONTROLLER's `existing_env`, character for character the predicate `roles/ssh_trust` uses — not the host this role happens to run against. |
| `preflight_attach_nodes_sharing_zone` | derived | New nodes whose `cs_app_domain` is still `cs_app_zone` — set nowhere, or set equal to it, which are the same failure. |
| `preflight_wildcard_domain_overrides` | derived | Hosts setting the retired `controller_wildcard_domain` to anything but `cs_app_zone`. `roles/controller`'s own default is not in scope here, so anything found was written by an operator. |
| `preflight_verify_registries` | `true` | Every network read in this role, in one switch. False makes preflight purely local, for an air-gapped converge. |
| `preflight_docker_groups` | `controller`, `metrics`, `registry`, `nodes` | Groups whose hosts run a docker daemon. Mirrors the `hosts:` line of site.yml's "Docker hosts" play; a host outside them never logs in to a registry. |
| `preflight_verify_controller_image` | `true` | The image existence-and-tag check specifically; the credential probes stay on. |
| `preflight_registry_manifest_hint_404` / `_denied` | see `defaults/main.yml` | The two ways a manifest read fails, as operator-facing sentences. |
| `preflight_registry_credentials_hint` | see `defaults/main.yml` | What a `401` from the token endpoint means, as an operator-facing sentence. |
| `preflight_image_pin_hint` | see `defaults/main.yml` | Why a floating controller tag is refused, and how to override, as an operator-facing sentence. |
| `preflight_registry_manifest_accept` | OCI + docker, index + manifest | `Accept` header for the manifest read. Which type comes back depends on how the image was built and pushed. |

Every derived variable above references `hostvars`, so every assertion that
reads one is `run_once` and evaluates in the executing host's own context —
the only place such a variable is valid (docs/contracts.md §Variable scope).
The single exception is `hostvars[h].cs_app_domain`, which is safe to read
cross-host by construction and is why that name exists (§Variable scope
again); this role still only reads it `run_once`.

Consumed from elsewhere in the inventory (not owned by this role):
`site` and the `cs_site` / `cs_site_metrics_hosts` / `cs_site_backup_hosts`
derivations in `playbooks/group_vars/all/sites.yml`,
`app_domain`, `cs_app_zone` and `cs_app_domain`
(`playbooks/group_vars/all/app_domains.yml`), `cs_new_nodes`,
the retired `controller_wildcard_domain`,
`existing_env`, `secret_key_base`, `user_auth_secret`, `tailscale_authkey`,
`tailscale_enabled`, `cs_registry_logins` / `cs_controller_registry` (composed
in `playbooks/group_vars/all/registries.yml`), `controller_image_repo`,
`controller_image_tag`, `controller_image_allow_floating_tag`.

## Failure messages

Every assertion's `fail_msg` names the exact host and variable (or value)
that needs fixing, and where to fix it — no generic "preflight failed"
messages.
