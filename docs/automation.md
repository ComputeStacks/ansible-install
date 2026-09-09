# Automated / ephemeral deployments

How to drive these playbooks from an outer provisioner (terraform, opentofu,
a CI pipeline) that creates the VMs and the DNS records, runs `site.yml`,
asserts the environment came up, and destroys everything. Nothing in this
document is a separate mode — it is the same greenfield install with the
interactive conveniences switched off.

## The contract: what the outer layer must produce

The playbooks' entire input is one inventory directory. The outer layer emits:

* `hosts.yml` — the topology. The required host vars are asserted by
  `roles/preflight` (see `inventories/example/hosts.yml` for the full set):
  every node needs `hostname`, `primary_ip`, `public_ip`, `region`, `az`,
  `container_network`, `container_network_name`; every nameserver needs
  `powerdns_name`.
* `group_vars/all/main.yml` — the four domains (`cs_portal_domain`,
  `cs_metrics_domain`, `cs_registry_domain`, `cs_app_zone`), admin email,
  DNS driver, ACME settings.
* `group_vars/all/secrets.yml` — the secrets listed in
  `docs/install.md` §4. All of them can be `openssl rand -hex 32`-style
  throwaways except `secret_key_base`/`user_auth_secret`, which just need to
  be 64+ hex chars generated per environment. For an ephemeral environment a
  plaintext file is acceptable; for anything longer-lived, encrypt it and use
  `--vault-password-file` instead of `--ask-vault-pass`.

And, before the play starts, the DNS records from `docs/install.md` §3 — A
records for the portal/metrics/registry domains, NS delegation (plus glue)
for `cs_app_zone`. Created at the provider that hosts the parent zone they
are authoritative immediately; there is no propagation to wait for. Order
matters only one way: **records first, then ansible** — if anything resolves
a name before its record exists, the NXDOMAIN is negatively cached for the
zone's SOA-minimum TTL.

A per-run random subdomain (`<run-id>.test.example.com` with
`portal.<run-id>...` under it) sidesteps negative caching entirely and keeps
concurrent runs from colliding. Certificate transparency logs record every
issued name; with the staging CA below that concern disappears too.

The machine hostname is the outer layer's job (cloud-init `hostname:`, or
terraform user_data running `hostnamectl set-hostname`). It **must equal the
inventory `hostname` var** — single lowercase word, no dots. The inventory
var is what the controller knows the node as (enrollment looks the Node row
up by it) and what cs-agent's own `os.Hostname()` labels backups and tasks
with; the two agreeing is what keeps those the same node.

## The knobs

```yaml
# group_vars/all/main.yml
acme_ca: letsencrypt_test        # staging CA: untrusted chain, no real rate
                                 # limits. Production LE allows 5 duplicate
                                 # certs/week per name set — a redeploy loop
                                 # exhausts that in a day. roles/validate
                                 # relaxes TLS verification automatically for
                                 # the *test CAs.
controller_seed_confirm: false   # the seed's confirmation pause needs a tty
controller_seed_dry_run_first: false   # optional; stops the preview being
                                 # PRINTED. The DRY_RUN pass still RUNS -- it
                                 # is what gate G reads, and gate G fails the
                                 # run on load balancer domain drift. Neither
                                 # flag can switch that off.
```

```bash
# fresh VMs every run — pinning host keys is noise in CI
export ANSIBLE_HOST_KEY_CHECKING=False

ansible-playbook -i inventories/ci playbooks/site.yml \
  --vault-password-file .vault-pass   # or nothing, if secrets.yml is plaintext
```

* **Leave tailscale off** (no `tailscale_authkey` in the secrets): every run
  would otherwise enroll N devices into a real tailnet. If a tailnet run is
  ever wanted, use an *ephemeral* auth key so the devices remove themselves.
* **Port 80** must still reach the controller, metrics and registry hosts for
  HTTP-01 — the staging CA validates the same way. NAT'd test infra should
  use a DNS-01 provider instead (`docs/acme-providers.md`); the provider
  credential can be scoped to the delegated test zone.
* The **pass/fail gate is `roles/validate`**: it is the last play of
  `site.yml` and fails the run, so the playbook's exit code is the test
  result. `--tags validate` re-runs just the assertions.

## What a destroyed run leaves behind

Almost nothing: vault's PKI, the controller database and the borg
repositories all die with the VMs. The residue to manage in the outer layer:
the DNS records (terraform destroy), tailnet devices if tailscale was enabled
(ephemeral keys), and image-registry pull quotas — Docker Hub allows
anonymous pulls per IP per 6 hours, and a busy pipeline from one egress IP
will eventually throttle; authenticate the pulls or put a mirror in front
when it does.
