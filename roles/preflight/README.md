# preflight

Assert-only role that checks the inventory, the hosts, the registries and the
load balancer DNS before anything is built. It never changes a host — it only
fails loudly (or, for the tailscale case, warns) before any other role runs.
The registry and DNS checks read from the network, which is still not a change
to a host.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Every targeted host is reachable" (`tasks_from: reachable`) | `all` |
| `site.yml` | "Preflight validation" | `all` |
| `add-region.yml` | "Every targeted host is reachable" (`tasks_from: reachable`) | `all` |
| `add-region.yml` | "Preflight validation and attach-mode inventory checks" | `all` |
| `add-region.yml` (`tasks_from: attach`) | "Preflight validation and attach-mode inventory checks" | `all` |
| `firewall.yml` | "Every targeted host is reachable" (`tasks_from: reachable`) | `all` |
| `firewall.yml` | "Preflight validation" | `all` |
| `firewall.yml` (`tasks_from: attach`, attach inventory only) | "Preflight validation" | `all` |
| `upgrade-docker.yml` | "Every targeted node is reachable" (`tasks_from: reachable`) | `node_upgrade_targets` |
| `maintain-nodes.yml` | "Every targeted host is reachable" (`tasks_from: reachable`) | the nodes it maintains, the controller and their backup hosts |
| `maintain-nodes.yml` | "Preflight validation" (and `tasks_from: attach` on an attach inventory) | the same hosts |
| `upgrade-docker.yml` (`tasks_from: ssh_source`) | "Preflight every targeted node" | `node_upgrade_targets` |
| `unseal.yml` | not run | — |

The reachability play runs first in every playbook, with `gather_facts:
false`; the preflight play follows with `gather_facts: true` and
`any_errors_fatal: true`, so a host failing any check stops every host.
`firewall.yml` turns off the registry, load balancer DNS, data disk,
`public_ipv6` and backup export checks (`preflight_verify_registries`,
`preflight_check_app_domain_dns`, `preflight_check_node_storage`,
`preflight_check_public_ipv6`, `preflight_check_backup_export`), none of
which bear on a firewall change, and sets
`firewall_ssh_allow_this_run_permitted`, the one place the
[ssh source](#this-runs-ssh-source-tasksssh_sourceyml) opt-in is honored.

`maintain-nodes.yml` turns off the load balancer DNS, data disk (it checks
`ready` itself, as upgrade-docker.yml does) and controller image checks;
the registry logins stay on, because the converge logs in again inside a
maintenance hold.

`tasks/attach.yml` holds the attach-mode inventory checks: every host in
`controller`, `metrics`, `backup` or `registry` is flagged `existing_env`, no
node is. They are kept out of `main.yml` because a greenfield inventory fails
the first by design. (Tailnet membership needs no attach-only check:
`tasks/tailscale.yml`, run by `main.yml`, covers every playbook.)

**Attach mode:** runs on every host, existing ones included, and changes
nothing; the OS, data-disk, registry-login and controller-image checks skip
`existing_env` hosts, and the delegation check is skipped in attach mode.
**Requires:** — (it includes `roles/node_storage`'s read-only `probe` and pure
`classify` task files, and `roles/cs_agent`'s pure `export_check`, itself).

## Reachability (`tasks/reachable.yml`)

Ansible does not stop for an unreachable host. It prints `UNREACHABLE`,
removes the host from the play, carries on with the others, and every later
play that targets it matches nothing and is skipped — so a run that cannot
reach the backup server at fact gathering builds everything else and fails
much later, somewhere unrelated, or not at all. `tasks/reachable.yml` pings
every targeted host, keeping each one in the play whatever happens
(`ignore_unreachable`, `failed_when: false`, and `any_errors_fatal: false`
on the ping alone), then ONE `run_once` assert (`tasks/reachable_assert.yml`)
fails the run if any host did not return `pong`. The message names every such
host with its `ansible_host`, `ansible_port` and ssh's own error, and says what
to check: the address, `ansible_port` / `ssh_port`, any firewall in between.

The verdict is the `preflight_unreachable` filter
(`filter_plugins/preflight.py`). It walks `ansible_play_hosts_all`, so a host
ansible dropped before the ping ran — it has no result at all — is named too.
`tests/preflight_reachable.yml` proves it against canned ping results.

The include carries the `always` tag, so `--tags validate` runs it too.

After preflight, `ansible.cfg` sets `any_errors_fatal = True` for every play:
a host that fails or becomes unreachable later in the run stops every host
rather than being dropped. The two validate plays turn it off, since they
change nothing.

## What it asserts

- Every host is Ubuntu 26.04 amd64/x86_64 — **skipped** on hosts flagged
  `existing_env: true` (pre-existing v1 environments are Debian).
- Every node in `groups['nodes']` defines: `hostname`, `primary_ip`,
  `public_ip`, `region`, `az`, `container_network`, `container_network_name`,
  `docker_data_device`.
- **The container data disk.** Every new node's `docker_data_device` is an
  absolute path under `/dev/`; then each new (not `existing_env`) node runs
  `roles/node_storage`'s read-only `probe` and pure `classify` with
  `node_storage_classify_fail: false`, and ONE `run_once` assert fails the run
  if any node's disk was refused, listing every refused node and its reason.
  A failed `run_once` task fails every host in the play, so one bad disk stops
  the whole run before any host is changed. A node built before the data-disk
  requirement has no such disk and is refused: it must be rebuilt.
- **`public_ipv6`, where a node sets it** (`tasks/public_ipv6.yml`, every
  node in `groups['nodes']`): one IPv6 host address in canonical RFC 5952
  form -- lowercase, no leading zeros, the longest zero run compressed, no
  prefix length or zone index -- and not unspecified, loopback, link-local,
  multicast, site-local or IPv4-mapped. The check is the `cs_ipv6_problem`
  filter (`filter_plugins/preflight_ipv6.py`, Python's `ipaddress`; no
  extra collection or `netaddr` needed), which also says what is wrong and,
  for a non-canonical address, what to write instead. Canonical form is
  required because roles/powerdns, roles/validate and this role compare the
  address as text against what PowerDNS and dig print. Unset, null and
  empty all mean "no IPv6 address".
- **Backup export to S3.** Each new node runs `roles/cs_agent`'s pure
  `export_check` with `cs_agent_export_check_fail: false`, and ONE `run_once`
  assert fails the run if any node's settings were refused, listing every
  node and its problems. Nodes with no `cs_agent_s3_export_bucket` (export
  off) always pass. The rules are in `roles/cs_agent/README.md`.
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
  - **In attach mode** (decided by the CONTROLLER's `existing_env`, not by the
    host the check runs against), every new node names its own
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
- **This run's ssh source** is covered by each narrowed host's ssh accept
  set, the ssh opt-in is used only where it is honored, `make lock-ssh`
  leaves no host open to anywhere, and no firewall rollback from an earlier
  run is pending — see
  [below](#this-runs-ssh-source-tasksssh_sourceyml).
- `secret_key_base` and `user_auth_secret` are defined, at least 128
  characters, and not the `CHANGEME` sample placeholders. **This role never
  generates these values** — they are immutable install inputs
  (docs/contracts.md rule 8); rotating them later destroys every encrypted
  credential in the controller.
- Tailnet membership (`tasks/tailscale.yml`, docs/contracts.md §Tailnet
  membership): a host with a non-empty `tailscale_authkey` and no
  `tailscale_enabled` is refused — under the old rules it was a member, under
  the new default (`false`) it is not, so the operator must say which. When
  some hosts are members and others are not, the non-members are reported
  via a `debug` warning (not a failure) — same-L2 regions legitimately stay
  off, but the operator should see the list and confirm it's intentional.
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

- **Load balancer DNS**, from the control machine, before anything is built —
  one of two checks, depending on who writes each az's pair
  (`<cs_app_domain> A <public_ip>`, `*.<cs_app_domain> CNAME <cs_app_domain>.`).
  Both are turned off by `preflight_check_app_domain_dns: false`, for a
  split-horizon control machine or records published after the converge. See
  below. Neither judges whether an address is publicly routable, so a
  private-addressed test environment passes.

## This run's ssh source (`tasks/ssh_source.yml`)

A narrowed `firewall_allow_ssh_from` locks out every source it does not cover
as soon as roles/firewall applies the table, and the connection the run is
using survives the apply, so nothing in the run would notice before its next
connection. Included from `tasks/main.yml` (so `site.yml`, `add-region.yml`
and `firewall.yml`) and from `upgrade-docker.yml`, whose reboots reconnect
through the persistent table, it checks:

- **`firewall_ssh_allow_this_run` is refused** unless the playbook sets
  `firewall_ssh_allow_this_run_permitted` (only `firewall.yml` does). The
  allowance it grants lives in an nftables set that every reboot and every
  later apply empties, so anywhere else it would vanish mid-run. With the
  opt-in, `firewall_ssh_run_allow_timeout` must be an nft duration (`1h`,
  `30m`, `1h30m`).
- **`make lock-ssh`** (`firewall_require_ssh_allowlist`) refuses every v2
  host (not `existing_env`) whose `firewall_allow_ssh_from` is empty.
- **The source.** On each v2 host whose list is non-empty, the run reads
  `SSH_CONNECTION` on the host — the address the host really sees, after any
  NAT, jump host or bastion — and records `preflight_ssh_source` (an
  IPv4-mapped IPv6 address unwrapped to IPv4) and `preflight_ssh_source_port`
  as facts; roles/firewall uses the source for the opt-in. Unless the opt-in
  is on, the run stops if the host's **effective** ssh accept set does not
  cover the source: the list, the peers the table adds (the controller's
  `primary_ip` / `public_ip` on nodes and the registry, every node's on a
  backup server), `firewall_extra_allowed_ipv4` / `_ipv6`, and loopback. The
  tailnet accepts and `firewall_extra_input_rules` are not counted, so the
  check can only refuse a run the table would let in, never the reverse. A
  list entry that is not an address or a network is named in the message and
  counts for nothing.
- **A pending firewall rollback.** On every v2 host, list or no list, the run
  reads `systemctl list-timers --all 'cs-firewall-rollback-*'` and stops if
  a rollback roles/firewall scheduled in an earlier run is still waiting to
  fire: that run never verified its table, and the timer replaces it
  whatever this run does. The message names the unit, when it fires, and
  how to cancel it once you have confirmed the running table lets you in.

Every message names the host, the source and the fix, and ends "Nothing was
changed." The decisions are `filter_plugins/preflight_ssh.py`
(`preflight_ssh_connection`, `preflight_ssh_covered`,
`preflight_ssh_unparsed`, python `ipaddress`; `preflight_rollback_pending`); `tests/firewall_ssh_source.yml`
proves them, and renders every host's real table to prove that the filter and
the template agree on which sources are accepted.

## The load balancer DNS checks

**Hand-made records (`tasks/app_domain_dns.yml`).** When the DNS driver is
not the bundled PowerDNS, or `powerdns_manage_lb_records` is off, the operator
publishes each new az's pair before the run (docs/install.md §3). Preflight
resolves both publicly: the apex must include that node's `public_ip`, and
`<preflight_app_domain_probe_label>.<cs_app_domain>` must be a CNAME to the
apex. The failure message names the exact record to create. It also resolves
the apex AAAA and **warns** (does not fail) about any address that is not the
`public_ipv6` of a node on that name: the controller rejects an apex address
that is not one of the load balancer's public addresses, but one an operator
added on the controller by hand is invisible from here.

**Records roles/powerdns writes (`tasks/app_zone_delegation.yml`).** With
`dns_driver: powerdns`, roles/powerdns writes the pair into the bundled tenant
zone early in the run, so there is nothing public to dig for yet — the check
above could only fail. What the operator still owns, and what can already be
wrong, is the **delegation** of `cs_app_zone` at its parent. Preflight finds
the parent's nameservers (`dig +short NS` on `cs_app_zone` minus one label,
walking further up past empty non-terminals, bounded by the zone's labels),
asks up to `preflight_app_zone_delegation_servers` of them **non-recursively**
for `cs_app_zone NS`, and requires every one that answers to hand out exactly
the set of `powerdns_name` across `groups['nameservers']` (lowercase,
trailing dot ignored). Three failures are kept apart: the parent's
nameservers could not be found (a lookup problem on this machine), none of
them answered (reachability), and the delegation is missing or names other
servers — only the last lists the NS records to create at the parent. Glue is
mentioned when a nameserver's name lies under the zone, but not checked.

It is **skipped in attach mode** (`preflight_attach_mode`, the controller's
`existing_env`): the zone is already live and delegated, and this run neither
made nor can change that. The records themselves are checked at the end of
either run by roles/validate — `dns` on every nameserver and
`lb_domain_public` through public resolution — and a one-line `debug` here
says so.

"Managed" is decided by `preflight_lb_records_managed`, the same expression,
spelled the same way, that roles/powerdns and roles/validate use:

```yaml
dns_driver | default('none') == 'powerdns'
and groups['ns_primary'] | default([]) | length > 0
and (hostvars[groups['ns_primary'][0]].powerdns_manage_lb_records | default(true)) | bool
```

To go back to hand-made records with the bundled PowerDNS, set
`powerdns_manage_lb_records: false` **on the nameservers**
(`group_vars/nameservers` or `ns_primary`'s host vars) — that is where
roles/powerdns reads it, and the only place this role looks.

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
| `preflight_reachable_hosts` / `preflight_reachable_hostvars` | `ansible_play_hosts_all` / `hostvars` | What `tasks/reachable_assert.yml` judges. Set only by `tests/preflight_reachable.yml`, to feed it canned ping results. |
| `preflight_required_node_vars` | see `defaults/main.yml` | List of host vars every node must define. |
| `preflight_check_node_storage` | `true` | The data disk probe and its run-wide assert. False only for a run that never touches docker or the disk (`firewall.yml`). |
| `preflight_check_public_ipv6` | `true` | The `public_ipv6` format check. False only in `firewall.yml`, which never reads it. |
| `preflight_check_backup_export` | `true` | The backup export check and its run-wide assert. False only in `firewall.yml`, which never configures cs-agent. |
| `preflight_storage_refused_nodes` | derived | Nodes whose `node_storage_state` fact is `refuse`. A plain fact, so the cross-host read is safe (docs/contracts.md rule 10). |
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
| `preflight_attach_mode` | derived | The CONTROLLER's `existing_env` — not the host this role happens to run against. |
| `preflight_attach_nodes_sharing_zone` | derived | New nodes whose `cs_app_domain` is still `cs_app_zone` — set nowhere, or set equal to it, which are the same failure. |
| `preflight_wildcard_domain_overrides` | derived | Hosts setting the retired `controller_wildcard_domain` to anything but `cs_app_zone`. `roles/controller`'s own default is not in scope here, so anything found was written by an operator. |
| `preflight_check_app_domain_dns` | `true` | Both load balancer DNS checks — the public record dig (hand-made records) and the delegation check (records written by roles/powerdns). False for split horizon or records published after the run. |
| `preflight_app_domain_probe_label` | `cs-preflight-probe` | Label queried under `*.<cs_app_domain>` to exercise the wildcard. Must not exist as a real record. |
| `preflight_lb_records_managed` | derived | Whether roles/powerdns writes the load balancer records this run. Decides which DNS check runs. Override `powerdns_manage_lb_records` on the nameservers, not this. |
| `preflight_app_zone_delegation_servers` | `3` | How many of the parent zone's nameservers are asked for the referral; every one that answers must agree. |
| `preflight_verify_registries` | `true` | Every network read in this role, in one switch. False makes preflight purely local, for an air-gapped converge. |
| `preflight_docker_groups` | `controller`, `metrics`, `registry`, `nodes` | Groups whose hosts run a docker daemon. Mirrors the `hosts:` line of site.yml's "Docker hosts" play; a host outside them never logs in to a registry. |
| `preflight_verify_controller_image` | `true` | The image existence-and-tag check specifically; the credential probes stay on. |
| `preflight_registry_manifest_hint_404` / `_denied` | see `defaults/main.yml` | The two ways a manifest read fails, as operator-facing sentences. |
| `preflight_registry_credentials_hint` | see `defaults/main.yml` | What a `401` from the token endpoint means, as an operator-facing sentence. |
| `preflight_image_pin_hint` | see `defaults/main.yml` | Why a floating controller tag is refused, and how to override, as an operator-facing sentence. |
| `preflight_registry_manifest_accept` | OCI + docker, index + manifest | `Accept` header for the manifest read. Which type comes back depends on how the image was built and pushed. |
| `preflight_ssh_allow_from` | `firewall_allow_ssh_from` | The list the ssh source check reads. |
| `preflight_ssh_peer_hosts` / `preflight_ssh_also` | derived | The peers roles/firewall accepts ssh from on this host, and every address besides the list the check counts. Read only on the executing host. |
| `preflight_ssh_unparsed` | derived | List entries that are not an address or a network. |

Every derived variable above references `hostvars`, so each is read only in
the executing host's own context — the only place such a variable is valid
(docs/contracts.md rule 10, §Variable scope): most in `run_once` assertions,
and `preflight_ssh_peer_hosts` / `preflight_ssh_also` in the per-host ssh
source check, for the host it runs on.
The single exception is `hostvars[h].cs_app_domain`, which is safe to read
cross-host by construction and is why that name exists (§Variable scope
again); this role still only reads it `run_once`.

Consumed from elsewhere in the inventory (not owned by this role):
`site` and the `cs_site` / `cs_site_metrics_hosts` / `cs_site_backup_hosts`
derivations in `playbooks/group_vars/all/sites.yml`,
`app_domain`, `public_ipv6`, `cs_app_zone` and `cs_app_domain`
(`playbooks/group_vars/all/app_domains.yml`), `cs_new_nodes`,
the retired `controller_wildcard_domain`, `dns_driver`, `powerdns_name` and
`powerdns_manage_lb_records` (on the nameservers),
`existing_env`, `secret_key_base`, `user_auth_secret`, `tailscale_authkey`,
`tailscale_enabled`, `cs_registry_logins` / `cs_controller_registry` (composed
in `playbooks/group_vars/all/registries.yml`), `controller_image_repo`,
`controller_image_tag`, `controller_image_allow_floating_tag`,
`firewall_allow_ssh_from`, `firewall_extra_allowed_ipv4` / `_ipv6`,
`firewall_require_ssh_allowlist`, `firewall_ssh_allow_this_run`,
`firewall_ssh_allow_this_run_permitted`, `firewall_ssh_run_allow_timeout`.

## Failure messages

Every assertion's `fail_msg` names the exact host and variable (or value)
that needs fixing, and where to fix it — no generic "preflight failed"
messages.
