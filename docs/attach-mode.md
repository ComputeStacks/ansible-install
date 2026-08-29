# Attach mode: a new region against an existing environment

`playbooks/add-region.yml` provisions a new region, availability zone and node
against an environment that already exists — typically one built by the v1
playbooks, on Debian, with a containerized metrics stack and an iptables
firewall.

It builds the new node in full, exactly as `site.yml` would. On the existing
shared hosts it performs **only** the write set enumerated below, and nothing
else. That restriction is not politeness: in attach mode the inventory is a
*partial view of the fleet*, so any file rendered whole from it would silently
drop every region this inventory does not happen to list — the prometheus
scrape configuration and the firewall are the two that would take a running
environment down.

If you are extending a **v2** environment, you do not need this playbook. Add
the node to the same inventory and re-run `site.yml`, optionally
`--limit`-scoped.

## Prerequisites

**The existing controller must already be upgraded** to a release that carries
`rake bootstrap:apply` — the manifest apply this playbook seeds through. Do
that first, the normal way:

```bash
cstacks upgrade
```

**`secret_key_base` in your vaulted `secrets.yml` must be the existing
controller's value, byte for byte.** Copy it out of
`/etc/default/computestacks` on the controller. Never the other way around:
the manifest apply encrypts with this value, and applying under a different
one writes credentials the running controller decrypts to `nil` — silent,
total credential loss that a `pg_dump` does not protect you from, because the
damage happens after the dump. The controller prep play asserts the match and
aborts if it fails.

**Three inputs describe the existing backup server** and have no safe default,
because v2's defaults are not what a v1 server has:

| Var | v1 value | Why it matters |
| --- | --- | --- |
| `backup_host_path` | `/mnt` | Where repositories live on that server. |
| `backup_borg_remote_path` | `/usr/bin/borg` | v2's default is `/opt/computestacks/bin/borg`, which does not exist there — every backup on the new node would fail with "command not found". |
| `backups_key` (secrets) | the environment's existing value | The borg passphrase. A new one makes the node's repositories unreadable alongside everyone else's. |

**Everything else in the inventory must describe the environment that
exists, not a new one.** Attach mode never re-renders a shared host's
configuration, so a value that disagrees with the running environment is not
corrected — it just fails, usually somewhere unhelpful:

* `cs_portal_domain`, `cs_metrics_domain`, `cs_registry_domain` and
  `cs_app_zone` are the existing environment's domains. The metric and log
  clients are matched by exact endpoint string, which is built from
  `cs_metrics_domain` and `cs_ports.metrics_*`.
* `prometheus_basic_auth_password` and `loki_basic_auth_password` are the
  existing metrics host's credentials. The htpasswd files there are not
  rewritten, so a fresh password means the new node's fluentd cannot ship
  logs and the controller cannot read metrics.
* `cs_admin_email` / `cs_admin_password` are still asserted present even
  though an attach manifest carries no `admin_user` section. Any valid value
  will do; the existing admin account is not touched.

**Flag every existing host** in the inventory:

```yaml
controller:
  hosts:
    ctl1:
      ansible_host: 192.0.2.10
      hostname: ctl1
      primary_ip: 10.100.0.10
      public_ip: 192.0.2.10
      existing_env: true
```

The playbook asserts this: an unflagged shared host stops the run with a
pointer to `site.yml`. The new node must **not** be flagged.

**Tailscale is off by default.** An existing v1 controller and metrics host
are not on a tailnet, and joining them is an operator action taken *before*
this playbook runs. If `tailscale_authkey` is set in the secrets, the playbook
requires every host in the run to declare `tailscale_enabled` explicitly —
otherwise the firewall, the prometheus fragment and the manifest would all
derive tailnet addresses for a tailnet nobody joined. Set
`tailscale_enabled: false` on the existing hosts (and on the new node, unless
you really are extending a tailnet).

## What it writes on the existing hosts

Exactly this, and nothing else:

| Host | Write | Notes |
| --- | --- | --- |
| controller | `cstacks database-backup` | The gate. Everything after it writes to the database. |
| controller | `NODE_ENROLLMENT_TOKEN` and `CS_PROXY_IPS_PATH` appended to `/etc/default/computestacks` | `lineinfile`, append-only, only when the key is absent. No `regexp`, so an existing value can never be rewritten. |
| controller | the v2 `cstacks` script | One of the two whole-file exceptions. It holds no environment-specific values and adds the `seed`, `runner` and `database-backup` subcommands plus the proxy_ips mount. |
| controller | portal container recreated | ~30 seconds of downtime — see below. |
| controller | firewall: one blanket accept per new node address | v1's own `lineinfile` idiom against `/usr/local/bin/cs-recover_iptables`, plus the same rules made live. |
| metrics | `/etc/prometheus/{node_exporter,cadvisor,haproxy}/<az>.yml` | New per-AZ fragment files, in v1's exact paths. Prometheus picks them up within one scrape interval; nothing is restarted. The other whole-file exception. |
| metrics | firewall: the same node-address appends | |
| backup | the `cstacks` account, its `~/.ssh`, and the repository path's ownership | v2's borg is **not** installed over the server's existing one. |
| backup | one `authorized_keys` entry for the new node | Added by `cs_agent`, commented with the node's hostname. |
| backup | firewall: the same node-address appends | Latent on most v1 servers — their script's `default_allow_ssh` accepts 22 from anywhere, so borg already reaches them. On a server built with `default_allow_ssh: false` the appends are what keeps the new node's backups from failing silently. |
| vault (on the controller) | nothing | The new node's docker certificate is *issued* from the existing PKI; the playbook unseals the vault if it is sealed and writes nothing. |

`/etc/default/computestacks` is append-only, always. No shared-host file is
ever re-rendered.

### The portal restart

Appending the two environment keys and installing the v2 `cstacks` script both
notify a portal recreate, and the prep play flushes that handler immediately —
`controller_seed` runs next and needs a portal that carries
`NODE_ENROLLMENT_TOKEN` and the proxy_ips mount. The portal is down for
roughly the time `docker run` takes, usually under a minute. **Schedule the
run accordingly**: tenant sites keep serving (they run on the nodes), but the
portal, the API and the job queues are unavailable while it restarts.

## Running it

```bash
ansible-playbook -i inventories/prod playbooks/add-region.yml --ask-vault-pass
```

The run stops and waits once, on purpose. `controller_seed` applies the
manifest with `DRY_RUN=1` first, prints the diff, and pauses for you to read
it. That is the last point before anything is written to the controller's
database. Set `controller_seed_confirm: false` to skip the prompt in CI (the
`pause` module needs a tty), and `controller_seed_dry_run_first: false` to
skip the preview entirely — both are deliberate opt-outs, not defaults.

The attach manifest carries **topology only**: the new location, region, node,
network and load balancer, plus the user-group region link without which the
new region is invisible to every existing user. It carries no settings, no DNS
driver, no products and no admin user, so nothing global is touched.
`controller_seed_full_manifest: true` renders the whole greenfield document
against the live controller instead — that rewrites settings and the DNS
driver, so do it deliberately or not at all.

The regions still reference the existing metric and log clients **by exact
endpoint string**, and a miss aborts the apply rather than creating a second
client. That is wanted: a duplicate client with different credentials splits
the placement metrics and every order in the new region is rejected for "no
capacity" with nothing else to show for it. If it aborts, reconcile
`controller_seed_metric_endpoint` / `_log_endpoint` with what the controller
actually has.

## After the run

`validate` runs against the controller and the new node only — the existing
metrics and backup hosts were not built by these playbooks and their unit set
is not this repository's to assert. To re-run just the checks later, use
`add-region.yml --tags validate` (`make add-region-validate ENV=<name>`), not
`site.yml`. Two of its checks matter most here:

* **borg version parity.** The client half of every backup is the
  `cs-docker-borg` image; the server half is whatever borg the existing
  server has. The check compares them and fails on a mismatch, because a
  mismatch corrupts repositories in ways that only surface at restore time.
  Fix the pair rather than skipping the check;
  `validate_borg_version_compare: false` downgrades it to a warning if you
  are knowingly running a mixed fleet.
* **the prometheus label contract.** It runs the controller's own placement
  query against the new node's labels. An empty result is the failure that
  rejects every order in the new region while every dashboard stays green.

Then confirm in the portal that the new Location/Region/Node exist and the
node reports capacity.

## What attach mode is not

It does not migrate an existing environment to v2. The existing hosts keep
their v1 configuration, their v1 firewall and their v1 packages; only the
`cstacks` script and the two appended environment keys change, and both are
additive. Converging old hosts onto the v2 roles is out of scope —
`site.yml` against a v1 host would re-render files it does not fully
describe, which is exactly what the flag exists to prevent.
