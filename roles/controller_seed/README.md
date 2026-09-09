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

1. Asserts the inputs: the domains, **one NEW node minimum** (`cs_new_nodes`,
   see "Sites" below), the admin account, the metric/log client credentials
   **for every site this run seeds**, the region tunables (against the
   *model's* validations), the required inventory vars on each node it will
   render, a single registry host, a dns driver it can seed, and — for
   PowerDNS — the API keys and a unique `powerdns_name` per nameserver. It
   also fails on a retired `controller_seed_{prometheus,loki}_{username,password}`
   or `controller_seed_lb_domain` still set in an inventory, rather than
   ignoring it silently.
2. Reads **one self-signed tenant wildcard `sharedcert.pem` per load balancer
   domain** — stat, assert, slurp, map, all keyed on `cs_app_domains`
   (`no_log` from the slurp onwards).
3. Renders `templates/manifest.yml.j2` **in memory** and asserts it parses as
   YAML, carries `schema_version: 1`, has at least one location, and uses no
   top-level key outside `vars/main.yml`'s frozen section list.
4. Writes it to `/var/lib/computestacks/manifest.yml`, root `0600`,
   `diff: false` + `no_log` (it carries the admin password, the DNS API keys,
   the client credentials, the stats password and the load balancer's private
   key).
5. Runs `DRY_RUN=1 cstacks seed` — **always**, because gate G reads it — and
   **fails the run if the preview reports drift on a load balancer `domain`**
   (see "Create-only, and the domain gate"). In attach mode, or with
   `controller_seed_update_addresses`, it also prints what it would create,
   any drift warnings and any `[rotate]` lines (paired credentials — client
   basic-auth, DNS API keys, LB shared certificate/stats password — are the
   ONE class the apply converges on existing rows; everything else is
   create-if-absent), and `pause`s for the operator.
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
| `app_domain` host var, else `cs_app_zone` | `…load_balancer.domain` **and** the CN of `…load_balancer.shared_certificate` | **Per az**, through the cross-host-safe `hostvars[host].cs_app_domain`. One load balancer per az, and in a real estate every one of them answers on a different domain. Both fields are keyed off the same value so they cannot disagree — a mismatch is a failed TLS handshake for every tenant container in that az. |
| `site` host var | *(nothing — the controller has no column for it)* | Decides **which** client each az names. One `metric_clients[]`/`log_clients[]` entry per site this run seeds nodes into, deduped by endpoint. |
| that site's `metrics_domain`, else `cs_metrics_domain`, + `cs_ports.metrics_*` | `metric_clients[].endpoint` / `log_clients[].endpoint` | Regions reference them by **exact endpoint string**. A single-site inventory renders the same one-element lists it always did. |
| `dns_driver: powerdns` + `groups['nameservers']` | `dns.driver` / `dns.zones` | Omitted entirely when `dns_driver: none`. |

## Sites, and which nodes reach the apply

`Region belongs_to :metric_client` / `:log_client` in the controller, and the
manifest takes `metric_clients:` / `log_clients:` as **lists** with each region
naming its own by exact endpoint string. Production has been in that shape for
years — two metrics servers across four Locations. The provisioner was the only
thing flattening it to one.

* **The client lists carry one entry per SITE this run seeds nodes into,
  deduped by endpoint string.** The apply matches a client on the exact
  string, so two sites resolving to the same endpoint must produce one row,
  not two: two rows with the same endpoint would leave regions pointing at the
  first and split the placement metrics the scheduler reads.
* **Each az names its own site's endpoints** —
  `loki_endpoint`, `metric_client_endpoint`, `log_client_endpoint` come from
  `cs_site_metrics_domains[hostvars[host].cs_site]`. The site MAPS are read
  **locally**, here on the controller, and keyed by the cross-host-safe
  `cs_site`; `hostvars[h].cs_site_metrics_domains` is silently Undefined and is
  a hard-rule violation (docs/contracts.md §Variable scope, rule 10).
* **Credentials are per site**, with the guard on the SUBSCRIPT and never the
  field: `(cs_site_metrics_credentials[<site>] | default(cs_metrics_credentials_default))`.
  The two production metrics servers do not share a password, and a wrong
  username is a 401 with exactly the same symptom as a wrong password.
* **When two sites do collide on an endpoint**, the first site in sorted order
  supplies the credentials. They are naming the same physical nginx vhost, so
  disagreeing about its basic-auth is a misconfiguration of the metrics hosts,
  not something this template can resolve — and `roles/preflight` is where a
  site's metrics host is validated.
* **A site with no metrics host in the inventory** falls back to
  `controller_seed_{metric,log}_endpoint`, the single-site defaults. An
  inventory that never mentions `site` is exactly that case, and renders the
  manifest it always rendered, byte for byte.

### `cs_new_nodes`, never `groups['nodes']`

The `locations:` loop, the `metric_clients`/`log_clients` site list, the
required-node-var assert and the `acme_server` warning all iterate
**`cs_new_nodes`** — `groups['nodes']` minus every host flagged `existing_env`.

An already-provisioned node must not reach the apply at all.
`Bootstrap::Writer#create_or_report!` **rotates** a load balancer's
`stats_password` and `shared_certificate` on a row that already exists
(controller repo `doc/bootstrap_manifest.md`, *Credential rotation*) — they
are the narrow enumerated exception to bootstrap-only, and there is no flag
for it and no preview beyond the DRY_RUN text.

Not *unconditionally*, to be exact: `Writer#rotate!` compares each credential
through the **decrypted reader** and skips one whose value has not actually
changed. That is what makes generate-once-at-a-stable-path idempotent — and it
is *not* what would make rendering an existing az safe, because nothing
guarantees the rendered value matches what that row already holds. A v1-seeded
az carries a certificate this repository never generated, and the stats
password is derived from `secret_key_base`. So rendering an existing az here
pushes a **new certificate and a new haproxy stats password to a production
node** at its next load balancer update. `--limit` does not protect against
this: it removes hosts from *plays*, not from `groups[]`, and this document is
rendered from `groups[]`.

`difference` does not preserve inventory order, so every consumer sorts —
manifest output order is visible in the DRY_RUN diff.

If every node in the inventory is an existing one there is nothing to seed and
the role fails at its first assert, rather than rendering a `locations:` list
of empty regions.

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
whether that address routes from a given region. This only actually lands on
a region seeded for the **first time** by this apply — an az that already
exists on the controller keeps its own `acme_server` regardless of what the
manifest renders; the rendered value is only compared against it and reported
as drift if the two disagree. `roles/validate`'s
`acme_backend` check probes the same derived address from each node and does
fail, so the hazard is caught at install time rather than by the first tenant
certificate that never issues.

The address value uses the blessed facts exception
(`hostvars[h].tailscale_ip | default(hostvars[h].ansible_local.computestacks.tailscale_ip | default(''))`);
tailnet *membership* stays a pure inventory predicate.

The `ansible_local` half reaches hosts outside a `--limit` only through the
repo's persistent fact cache (`ansible.cfg`, `.ansible_facts_cache/`), which
is gitignored. **Guard: the first-ever run from a fresh operator clone must be
un-limited** — otherwise the manifest is seeded with the `primary_ip` fallback
for every un-targeted tailnet node, and the role's warning is the only signal.

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
* **`load_balancer.domain` is PER AZ** — `hostvars[host].cs_app_domain`, which
  is the node's `app_domain` host var falling back to `cs_app_zone`. Every az
  has its own load balancer and in a real estate every one of them answers on
  a different domain; production runs nine. v1 had two variables — `cs_app_url`
  for the LB domain and the wildcard certificate's CN, and `cs_app_zone` for
  the `Dns::Zone` that contains it — and v2 collapsed them into `cs_app_zone`,
  which is the bug this un-does. `cs_app_zone` keeps its own meaning: **one**
  parent `Dns::Zone`, one `dns.zones` entry, unchanged. The controller's
  `LetsEncryptAuth#dns_zone` walks *up* from a container FQDN and takes the
  broadest `Dns::Zone` that matches, so one parent zone serves every LB domain
  beneath it.
* **`shared_certificate` is looked up by that same domain.** The LB domain
  **must** equal the CN/SAN of the certificate it serves or every tenant TLS
  handshake in that az mismatches, so there is deliberately no second variable
  that could disagree: `cs_app_domain_cert_paths[<that domain>]` (`vars/main.yml`)
  names the pem `roles/controller` generated *for that domain*. The map is
  read **locally**, here on the controller, and keyed by the cross-host-safe
  `cs_app_domain`; `hostvars[h].cs_app_domain_cert_paths` is silently
  Undefined and a hard-rule violation (docs/contracts.md §Variable scope, rule
  10). Path scheme:

      domain == cs_app_zone  ->  controller_seed_shared_cert_path   (LEGACY)
      otherwise              ->  <cs_app_wildcard_dir>/<domain>/sharedcert.pem

  The legacy carve-out is load bearing. `Writer#rotate!` skips a credential
  whose decrypted value already matches, so generate-once at a **stable** path
  re-seeds as a no-op; move the default domain's certificate to a new path and
  the value genuinely changes, and the next converge pushes a fresh
  certificate to every load balancer in the fleet.
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

## Create-only, and the domain gate

**The per-az domain lands when the `LoadBalancer` row is CREATED, and only
then.** `Bootstrap::ApplyService#apply_load_balancer` splits the row's fields
two ways, and everything below follows from that split:

* `domain` goes in `attrs`, so on an **existing** row `create_or_report!`
  calls `report_drift` and never writes it. No `addresses:` hash is passed
  either, so `UPDATE_ADDRESSES=1` does not reach it (the schema doc says so
  explicitly). A live load balancer's domain is operator-owned — it is edited
  by humans in the admin UI for years, and a stale manifest must not roll that
  back.
* `shared_certificate` goes in `credential_secrets`, and `rotate!` **does**
  write it whenever the decrypted value differs.

Left alone, an inventory whose `app_domain` disagrees with an already-seeded
row would therefore replace that load balancer's **certificate** and leave its
**domain** untouched — a production load balancer serving a wildcard whose CN
no longer matches the name it answers on, failing every tenant TLS handshake
in that az, inside a green run. That is this feature's own worst failure,
inverted.

So `tasks/main.yml` **fails the run** when the `DRY_RUN` preview reports drift
on a load balancer `domain`, before the real apply and before the confirmation
prompt. It names the load balancer and both values, verbatim from the apply's
own change log, and tells the operator to change the domain in the admin UI
first (or to correct `app_domain` to match what the database already holds).
The role will not do it for them.

The gate fires on **every** run, because the `DRY_RUN` preview it reads is
unconditional. Greenfield is not exempt, and that is the whole point: an
environment that has already converged still has nothing flagged
`existing_env`, so it is a greenfield inventory with live rows behind it, and
that is exactly where adding `app_domain` to a node would otherwise rotate a
certificate onto a load balancer whose domain the apply will not touch.
`controller_seed_dry_run_first` governs only whether the preview is
*printed*. **Changing the domain of an existing
load balancer is out of scope for the provisioner**, and this is a guard
against a future mistake rather than a migration tool: production's rows were
created by v1 with their correct per-region domains already.

## Attach mode (`existing_env: true`)

The manifest carries the **topology only** — `locations` with their regions,
nodes, networks and load balancers — plus `user_group: {link_regions: all}`,
without which the new region is invisible to every user in the default group.
No `settings`, no `dns`, no `metric_clients`/`log_clients`, no `products`,
`catalog`, `features` or `admin_user`. Set
`controller_seed_full_manifest: true` to render the whole greenfield document
against a live controller instead. Nothing in it is an overwrite there:
settings are seeded only while still unconfigured, and the DNS driver is
never reconfigured once it exists — a difference from the manifest is
reported as drift, not applied. Do it deliberately anyway: it is how a
still-unconfigured setting gets seeded, and how the fuller drift report gets
printed.

The regions still reference the metric/log clients **by endpoint**, now that
site's own endpoint. The match is exact and a miss aborts the apply — which is
the wanted behaviour: it means the existing controller's client endpoint is not
what this inventory says it is, and creating a second client would silently
split the placement metrics. Check the rendered strings against the live
`MetricClient.endpoint` / `LogClient.endpoint` character for character before
the run; in attach mode the client sections are not even emitted, so the
strings in the regions are all there is.

Hosts flagged `existing_env` are **not** in the document — see "`cs_new_nodes`,
never `groups['nodes']`" above. The attach inventory is a partial view by
convention (docs/contracts.md rule 9), and this is the belt to that braces.

**The new az needs its own `app_domain`, and its own certificate.** Attach
mode runs `roles/controller` with `tasks_from: attach_prep`, which generates
the wildcard for each *new* domain under `cs_app_wildcard_dir` and never
touches the legacy path — so the existing environment's certificate is left
exactly as it is, which is the whole point. v2 before this change read
whatever sat at the legacy path and seeded it as the new region's
`shared_certificate` while `domain:` rendered the environment-wide zone: a
certificate belonging to some other region, on a load balancer answering on a
name it does not cover. `roles/preflight` requires a new az's `app_domain` to
be set **and to differ from `cs_app_zone`** for this reason — setting it
*equal* would select the legacy path, generate nothing, and reproduce the bug
while passing the check.

**Every** run makes a `DRY_RUN=1` pass first, and it cannot be switched off:
gate G reads its output, and a gate whose input never ran passes without
checking anything. `DRY_RUN=1` writes nothing (the apply is one transaction
and the preview rolls it back), so the cost on a greenfield converge is one
extra read-only `cstacks seed`.

What is gated is the human-facing half. The diff is PRINTED, and the run
`pause`s, on an attach run or a run with `controller_seed_update_addresses`.
Set `controller_seed_confirm: false` in CI (the pause module needs a tty);
`controller_seed_dry_run_first: false` stops the diff being printed. Neither
flag disables gate G.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `controller_seed_manifest_path` | `/var/lib/computestacks/manifest.yml` | root 0600; deleted after a successful apply. |
| `controller_seed_remove_manifest` | `true` | Set false to keep the rendered document (it carries secrets). |
| `controller_seed_full_manifest` | `false` | Attach mode: render the global sections too. |
| `controller_seed_dry_run_first` / `_confirm` | `true` / `true` | Whether the preview is PRINTED, and whether the run pauses — both only in attach mode or on a run with the address flag below. Neither controls whether the `DRY_RUN` pass RUNS: it is unconditional, because gate G reads it. |
| `controller_seed_update_addresses` | `false` | **Deliberate topology changes only.** Runs the apply with `UPDATE_ADDRESSES=1`: `region.acme_server`, `node.agent_host` (incl. clearing) and the DNS driver endpoint are updated on existing rows, reported as `[readdress]` lines. Full run or warm fact cache only; the new path must already be up — the controller dials the new address on its next call. |
| `controller_seed_registry_node` | first registry host's `primary_ip` | `Setting.registry_node`. Empty ⇒ the registry settings are omitted. |
| `controller_seed_cr_le` | `{{ cs_registry_domain }}` | `Setting.cr_le`. |
| `controller_seed_settings_extra` | `{}` | Extra `Setting` rows (`company_name`, `app_name`, `general_support`, `acme_email`, …). **Seed-only** — written only while nobody has configured that setting yet, and an unknown name aborts the apply. |
| `controller_seed_metric_endpoint` / `_log_endpoint` | `https://{{ cs_metrics_domain }}:{{ cs_ports.metrics_prometheus / metrics_loki }}` | The **single-site fallback**, unchanged. Used for a site with no metrics host in the inventory — which is every host of an inventory that never mentions `site`. A site whose metrics host *is* in the inventory uses that host's own `metrics_domain` instead, so on an attach run this pair is normally **not** consulted at all. The apply matches a client on the exact endpoint string, and the lever for making that string match an existing row is `metrics_domain` on the metrics host plus `cs_ports.metrics_prometheus` / `_loki` — not these. If a live `MetricClient.endpoint` has a shape those cannot produce (a path, a trailing slash, plain http), stop and add an explicit per-site override rather than bending `cs_metrics_domain`, which also names the certificate and the loki push URL. |
| `controller_seed_prometheus_username` / `_password`, `controller_seed_loki_username` / `_password` | **retired** | The client basic-auth is per site now, with one spelling (docs/contracts.md §Site scoping). Set `prometheus_basic_auth_password` / `loki_basic_auth_password` and `acme_web_{prometheus,loki}_username` for the environment-wide values, and `metrics_site_credentials` in the vaulted secrets for a per-site override. `tasks/main.yml` **fails** if one of the four is still set, rather than ignoring it — the apply rotates a client's username/password on an existing row (`Writer#rotate!` skips a value that has not actually changed, but a dropped override is precisely a value that HAS changed), so a silently-dropped override would put the wrong password on a live controller. |
| `controller_seed_dns_endpoint` | `http://<ns_primary primary_ip>:{{ cs_ports.pdns_api }}/api/v1/servers` | Ends at `/servers`, NOT `/servers/localhost`: `powerdns-ruby` appends the server id itself, and the doubled path is a 404 the gem raises as `Pdns::UnknownObject`. |
| `controller_seed_pdns_zone_type` | `master` | `settings.config.zone_type`. |
| `controller_seed_p_net_size` | `27` | 24–29 (model validation). |
| `controller_seed_pid_limit` | `300` | v1 value. Column name is `pid_limit`, singular. |
| `controller_seed_ulimit_nofile_soft` / `_hard` | `2500` / `3000` | Hard must be ≥ soft — docker rejects every container start otherwise and **the model does not check it**, so this role does. |
| `controller_seed_lb_domain` | **retired** | It was ONE scalar rendered into EVERY az's load balancer, and every az's load balancer answers on a different domain — the bug this feature fixes. The domain is `cs_app_domain` per node now: set `app_domain` as a host var (a node that sets none takes `cs_app_zone`, which is what this defaulted to). `tasks/main.yml` **fails** if an inventory still sets it, rather than ignoring it — an operator who set it meant to choose the LB domain, and quietly rendering something else would publish tenant containers under a name their DNS does not answer for, with nothing in the run, the drift report or `validate` mentioning the variable. |
| `controller_seed_lb_stats_bind` | `*:{{ cs_ports.haproxy_stats }}` | Same value the metrics scrape and the firewall use. |
| `controller_seed_lb_stats_password` | `haproxy_stats_password`, else derived from `secret_key_base` | Never leave it at the column default. |
| `controller_seed_shared_cert_path` | `{{ controller_wildcard_cert \| default('/var/lib/computestacks/.ssl_wildcard/sharedcert.pem') }}` | The **legacy** path, and the one operator override inside `cs_app_domain_cert_paths`: it supplies the entry for the domain equal to `cs_app_zone`. Every *other* domain derives its path from `cs_app_wildcard_dir` and this variable does not touch them. v1 used this same path, which is why it is still the **attach-mode escape hatch** — on a controller with a non-default `controller_data_dir`, point it at that environment's existing shared certificate pem. (A new az attached to an existing environment should set its own `app_domain` and takes the per-domain path instead; `roles/preflight` asserts exactly that.) |
| `controller_seed_catalog_container_images` | `true` | Slow; an attach manifest omits the section entirely. |
| `controller_seed_admin_*` | from `cs_admin_email` / `cs_admin_password` | Create-if-absent only: re-running never resets a password. |

Consumed, not owned: `cs_portal_domain`, `cs_registry_domain`,
`cs_metrics_domain`, `cs_app_zone`, `cs_admin_email`, `cs_admin_password`,
`currency`, `dns_driver`, `pdns_api_key`, `pdns_web_key`,
`prometheus_basic_auth_password`, `loki_basic_auth_password`,
`secret_key_base`, `tailscale_authkey`, `existing_env`, `cs_ports`, the
site contract (`playbooks/group_vars/all/sites.yml`): `cs_site`,
`cs_site_metrics_domains`, `cs_site_metrics_credentials`,
`cs_metrics_credentials_default`, `cs_new_nodes` — and the app-domain contract
(`playbooks/group_vars/all/app_domains.yml`): `cs_app_domain` and
`cs_app_wildcard_dir`. The operator inputs behind those two are the per-node
`app_domain` host var and the environment-wide `app_wildcard_dir`.

`vars/main.yml` holds the frozen schema section list — a mirror of the
controller's `Bootstrap::Manifest::SECTIONS` — plus the pairwise tailnet
predicate, the two derived site values (`controller_seed_manifest_sites`,
`controller_seed_site_endpoints`) and the two derived app-domain values
(`cs_app_domains`, `cs_app_domain_cert_paths`). None of them are operator
knobs.

The last two are a **cross-role contract with frozen spellings**, defined the
same way in `roles/controller/vars/main.yml` — the role that *generates* the
pems these paths point at. The only line that differs is the `cs_app_zone`
entry, which that role takes from `controller_wildcard_cert` (a
`roles/controller` default, not in scope here or anywhere in attach mode) and
this one takes from `controller_seed_shared_cert_path`, whose fallback is the
identical literal. That is also why both carry a deliberate
`# noqa: var-naming[no-role-prefix]`: prefixing them per role would give one
contract two names in the two roles that must agree about it.

**Both maps are LOCAL USE ONLY.** They are built from `hostvars`, so
`hostvars[h].cs_app_domains` / `hostvars[h].cs_app_domain_cert_paths` evaluate
to Undefined across hosts — silently, and a `| default()` then turns that into
a wrong-but-plausible value (docs/contracts.md §Variable scope, rule 10). The
value that survives a cross-host read is `cs_app_domain`, which is trivial and
hostvars-free for exactly that reason, and it is the one the template uses per
node.

## Inventory requirements this role adds

* Every host in `groups['nameservers']` must define **`powerdns_name`** (its
  FQDN) in the inventory when `dns_driver: powerdns`. It becomes the zone's NS
  records. `roles/powerdns`'s default is a placeholder and role defaults are
  not visible from this play, so the value has to be inventory-level; the role
  asserts it is present and unique per nameserver.
* `haproxy_stats_password` is optional but recommended in the vaulted secrets.
* **`app_domain`** is optional per node and defaults to `cs_app_zone`, so no
  existing inventory needs an edit — but every distinct value it takes must
  have a pem on the controller before this role runs, or the per-domain
  certificate assert fails naming the path it wanted. `roles/controller`
  generates them; it generates **once per domain**, so a domain added to the
  inventory after the controller was built needs that role re-run.
  `roles/preflight` validates the shape (bare lowercase domain, no `*`, no
  scheme, not empty), that no two azs share one, and that each is at or under
  `cs_app_zone`.

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
