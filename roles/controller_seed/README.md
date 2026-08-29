# controller_seed

**Owner: Wave 4I.** `hosts: controller`.

## Purpose

Render the ComputeStacks **bootstrap manifest** from the inventory and apply
it with `cstacks seed` (`rake bootstrap:apply[<manifest>]`). This is the step
that creates the Locations, Regions, Nodes, Networks and Load Balancers — and
therefore the step that mints the agent tokens `roles/cs_agent` reads back
during enrolment. It runs **after every host is built and before the nodes
enrol** (`playbooks/site.yml`, "Seed controller").

The schema is the controller repo's `doc/bootstrap_manifest.md`,
`schema_version: 1`, **frozen**. An unknown key there is a hard error, not a
warning, so this role also validates the rendered document against the schema's
top-level key set before it writes anything.

It replaces v1's generated `bootstrap.rake` — embedded ruby inside a Jinja
template that drifted from the controller's models across releases.

## What it does, in order (`tasks/main.yml`)

1. Asserts the inputs: the domains, one node minimum, the admin account, the
   metric/log client credentials, the region tunables (against the *model's*
   validations), the required per-node inventory vars, a single registry host,
   a dns driver it can seed, and — for PowerDNS — the API keys and a unique
   `powerdns_name` per nameserver.
2. Reads the self-signed tenant wildcard `sharedcert.pem` (`no_log`).
3. Renders `templates/manifest.yml.j2` **in memory** and asserts it parses as
   YAML, carries `schema_version: 1`, has at least one location, and uses no
   top-level key outside `vars/main.yml`'s frozen section list.
4. Writes it to `/var/lib/computestacks/manifest.yml`, root `0600`,
   `diff: false` + `no_log` (it carries the admin password, the DNS API keys,
   the client credentials, the stats password and the load balancer's private
   key).
5. Attach mode only: runs `DRY_RUN=1 cstacks seed`, prints the diff, and
   `pause`s for the operator.
6. Runs `cstacks seed`, prints the change log, and **deletes the manifest**.
   A failed apply keeps the file on purpose and says so — the apply is one
   transaction, so nothing was written to the database and the rendered
   document is the thing to look at.

## Vocabulary mapping

The provisioner's vocabulary and the controller's are not the same words. This
role is the only place the mapping exists (docs/contracts.md §Vocabulary).

| Provisioner (inventory) | Controller (manifest / models) | Notes |
| --- | --- | --- |
| `region` host var (e.g. `exm001`) | `locations[].name` (`Location`) | One location per distinct `region` across `groups['nodes']`. |
| `az` host var (e.g. `exm-001`) | `locations[].regions[].name` (`Region`) | Exactly **one node per az** — preflight enforces it, this role relies on it. |
| node in `groups['nodes']` | `…regions[].nodes[]` (`Node`) | `hostname`/`label` from the `hostname` host var, never `ansible_hostname`. |
| `container_network` | `…regions[].networks[].subnet` | Must be RFC-1918 and at least a `/28`. |
| `container_network_name` | `…regions[].networks[].name` | Passed **raw**; the model normalises it (`net-exm-001` is stored as `netexm001`). The apply matches on the normalised form, so the raw value is stable and readable in the diff. |
| the az's single node | `…regions[].load_balancer` | `ext_ip`/`internal_ip` = that node's `primary_ip`, `public_ip` = its `public_ip`. |
| `cs_metrics_domain` + `cs_ports.metrics_*` | `metric_clients[]` / `log_clients[]` | Lists, one entry each; regions reference them by **exact endpoint string**. |
| `dns_driver: powerdns` + `groups['nameservers']` | `dns.driver` / `dns.zones` | Omitted entirely when `dns_driver: none`. |

## The two derivations worth reading twice

### `regions[].acme_server` — the controller address, per az

`host:port`, **no scheme**. It is the address **that az's node** dials the
controller's ACME backend on (`cs_ports.controller_acme_backend`, 3000):

* the **controller's** tailnet address when **both** the controller and that
  node are tailnet-joined (docs/contracts.md §Tailscale address derivation —
  the rule is pairwise, never per-node);
* otherwise the controller's `primary_ip`.

Set `controller_acme_address` as a host var on a node to override it — a
remote region with no tailnet needs an address that is actually routable from
there, and `primary_ip` may not be.

The role **warns** (never fails) when the fallback applies, naming every node
whose az would carry the controller's `primary_ip`: the role cannot know
whether that address routes from a given region. `roles/validate`'s
`acme_backend` check probes the same derived address from each node and does
fail, so the hazard is caught at install time rather than by the first tenant
certificate that never issues.

The address value uses the blessed facts exception
(`hostvars[h].tailscale_ip | default(hostvars[h].ansible_local.computestacks.tailscale_ip | default(''))`);
tailnet *membership* stays a pure inventory predicate.

### `nodes[].agent_host` — only when the controller is on the tailnet

`agent_host` overrides the address the **controller** dials cs-agent on, so it
is the *controller's* tailnet membership that decides it, not the node's:

* both ends tailnet-joined → the **node's** tailscale IP;
* otherwise the key is **omitted entirely**, and the controller falls back to
  `primary_ip`.

Omitted, not blank: the apply assigns every key that is present, so an empty
string would be stored as an empty `agent_host` rather than read as "unset".

## Derivation decisions

* **`cr_le` = `cs_registry_domain`.** v1's `bootstrap.rake` wrote
  `cs_portal_domain` into `cr_le` under a "Container Registry Settings"
  heading — a copy/paste bug. The schema doc ("Domain to use for the
  registry's ACME certificate") and `roles/registry`'s README both say the
  registry domain.
* **`registry_node` is the registry host's `primary_ip`**, not a domain: the
  controller SSHes to it (`DockerSSH`). The four registry settings are omitted
  when the inventory has no registry group.
* **The DNS keys look swapped and are not.** `ProvisionDriver#cloud_auth`
  builds `Pdns::Auth.new(0, username, api_key_column, api_secret_column)` and
  `Pdns::Auth` is `(user_id, username, password, api_key)`. So the PowerDNS
  **webserver password** (`pdns_web_key`) goes in `api_key`, and the **X-API-Key**
  (`pdns_api_key`) goes in `api_secret`. v1 did the same.
* **The PowerDNS API endpoint is the leader's `primary_ip`, never its tailnet
  address.** `roles/powerdns` writes the controller's `primary_ip`/`public_ip`
  into `webserver-allow-from`; a request from the controller's tailnet address
  would be rejected. (It is plain HTTP with an API key in a header — v1
  behaviour, unchanged here; see "Known gaps".)
* **`loki_endpoint` carries no credentials.** It is the *container* write path;
  `Region#loki_container_endpoint` splices the log client's username/password
  into it controller-side.
* **`load_balancer.domain` = `cs_app_zone`** (through
  `controller_wildcard_domain`). v1 had two variables — `cs_app_url` for the
  LB domain and the wildcard certificate's CN, and `cs_app_zone` for the
  `Dns::Zone` that contains it. v2 collapsed them into `cs_app_zone`
  (`roles/controller`: `controller_wildcard_domain: "{{ cs_app_zone }}"`), and
  the LB domain **must** equal the CN/SAN of the certificate it serves or
  every tenant TLS handshake mismatches. `controller_seed_lb_domain` defaults
  to `controller_wildcard_domain` for exactly that reason: one variable, both
  places.
* **`stats_password` is always set.** The column ships a hard-coded default,
  so leaving it unset publishes the same stats password on every install.
  Default: `haproxy_stats_password` if the operator sets it, otherwise a
  deterministic SHA-256 of `secret_key_base` + a fixed label (one-way, stable
  across converges, unique per install).
* **`port_begin`/`port_end` come from `cs_ports`**, not from the model's
  defaults — they must match the range `roles/firewall` opens on the node.
* **`node.active: true` explicitly.** The column defaults to `false` and
  `Node.available` filters on it: a node left at the column default accepts no
  orders and every order fails with "no capacity" and no other symptom.
* **`le: false`** on the load balancer: a shared certificate is supplied, and
  `le: true` makes the controller order a wildcard ACME certificate
  synchronously in an `after_save`.
* **No `consul_token`.** Dead column since the v3 agent cutover; v1 set it.

## Attach mode (`existing_env: true`)

The manifest carries the **topology only** — `locations` with their regions,
nodes, networks and load balancers — plus `user_group: {link_regions: all}`,
without which the new region is invisible to every user in the default group.
No `settings`, no `dns`, no `metric_clients`/`log_clients`, no `products`,
`catalog`, `features` or `admin_user`. Set
`controller_seed_full_manifest: true` to render the whole greenfield document
against a live controller; that rewrites settings and the DNS driver, so do it
deliberately.

The regions still reference the metric/log clients **by endpoint**. The match
is exact and a miss aborts the apply — which is the wanted behaviour: it means
the existing controller's client endpoint is not what this inventory says it
is, and creating a second client would silently split the placement metrics.

Attach mode always runs `DRY_RUN=1` first, prints the diff, and waits at a
`pause` prompt. Set `controller_seed_confirm: false` in CI (the pause module
needs a tty); `controller_seed_dry_run_first: false` skips the preview.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `controller_seed_manifest_path` | `/var/lib/computestacks/manifest.yml` | root 0600; deleted after a successful apply. |
| `controller_seed_remove_manifest` | `true` | Set false to keep the rendered document (it carries secrets). |
| `controller_seed_full_manifest` | `false` | Attach mode: render the global sections too. |
| `controller_seed_dry_run_first` / `_confirm` | `true` / `true` | Attach-mode preview and prompt. |
| `controller_seed_registry_node` | first registry host's `primary_ip` | `Setting.registry_node`. Empty ⇒ the registry settings are omitted. |
| `controller_seed_cr_le` | `{{ cs_registry_domain }}` | `Setting.cr_le`. |
| `controller_seed_settings_extra` | `{}` | Extra `Setting` rows (`company_name`, `app_name`, `general_support`, `acme_email`, …). **Update-only** — an unknown name aborts the apply. |
| `controller_seed_metric_endpoint` / `_log_endpoint` | `https://{{ cs_metrics_domain }}:{{ cs_ports.metrics_prometheus / metrics_loki }}` | Matched exactly by the apply. |
| `controller_seed_prometheus_username` / `_loki_username` | `promuser` / `loguser` | Follow `acme_web_*_username` when the operator overrides those. |
| `controller_seed_dns_endpoint` | `http://<ns_primary primary_ip>:{{ cs_ports.pdns_api }}/api/v1/servers/localhost` | Full PowerDNS API server path. |
| `controller_seed_pdns_zone_type` | `master` | `settings.config.zone_type`. |
| `controller_seed_p_net_size` | `27` | 24–29 (model validation). |
| `controller_seed_pid_limit` | `300` | v1 value. Column name is `pid_limit`, singular. |
| `controller_seed_ulimit_nofile_soft` / `_hard` | `2500` / `3000` | Hard must be ≥ soft — docker rejects every container start otherwise and **the model does not check it**, so this role does. |
| `controller_seed_lb_domain` | `{{ controller_wildcard_domain \| default(cs_app_zone) }}` | Must equal the wildcard certificate's CN. |
| `controller_seed_lb_stats_bind` | `*:{{ cs_ports.haproxy_stats }}` | Same value the metrics scrape and the firewall use. |
| `controller_seed_lb_stats_password` | `haproxy_stats_password`, else derived from `secret_key_base` | Never leave it at the column default. |
| `controller_seed_shared_cert_path` | `{{ controller_wildcard_cert \| default('/var/lib/computestacks/.ssl_wildcard/sharedcert.pem') }}` | v1 used the same path, so attach mode reads the existing environment's certificate. |
| `controller_seed_catalog_container_images` | `true` | Slow; an attach manifest omits the section entirely. |
| `controller_seed_admin_*` | from `cs_admin_email` / `cs_admin_password` | Create-if-absent only: re-running never resets a password. |

Consumed, not owned: `cs_portal_domain`, `cs_registry_domain`,
`cs_metrics_domain`, `cs_app_zone`, `cs_admin_email`, `cs_admin_password`,
`currency`, `dns_driver`, `pdns_api_key`, `pdns_web_key`,
`prometheus_basic_auth_password`, `loki_basic_auth_password`,
`secret_key_base`, `tailscale_authkey`, `existing_env`, `cs_ports`.

`vars/main.yml` holds the frozen schema section list — a mirror of the
controller's `Bootstrap::Manifest::SECTIONS`, not an operator knob.

## Inventory requirements this role adds

* Every host in `groups['nameservers']` must define **`powerdns_name`** (its
  FQDN) in the inventory when `dns_driver: powerdns`. It becomes the zone's NS
  records. `roles/powerdns`'s default is a placeholder and role defaults are
  not visible from this play, so the value has to be inventory-level; the role
  asserts it is present and unique per nameserver.
* `haproxy_stats_password` is optional but recommended in the vaulted secrets.

## Known gaps / notes for the next wave

* **The PowerDNS API call is plain HTTP** (`http://<ip>:8081`) carrying the
  API key in a header, exactly as v1. It cannot use the tailnet address
  because `webserver-allow-from` lists the controller's `primary_ip`/
  `public_ip`. Worth revisiting as a pair (powerdns + controller_seed).
* **AutoDNS is not implemented.** `dns_driver` must be `powerdns` or `none`;
  anything else fails loudly rather than rendering half a driver.

## Requirements

`cstacks` must be installed and the portal image available (roles/controller,
or attach mode's `tasks/attach_prep.yml`). No collections beyond ansible-core.
