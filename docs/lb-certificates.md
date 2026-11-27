# Load balancer certificates

Each availability zone's load balancer answers on its `app_domain` and needs a
certificate for `<app_domain>` and `*.<app_domain>`. From controller 9.9 the
controller issues it itself, through its bundled cert-issuer (lego, DNS-01
challenge). The provisioner does not issue it and runs no DNS server: it gives
the controller a DNS credential, and you publish the DNS records.

Until the ACME certificate is active, the load balancer serves the self-signed
certificate the seed generated for the az.

## What you publish

For every az's `app_domain` (see [install.md §3](install.md#3-point-dns-at-the-hosts)):

```
exm-001.usercontent.example.com.    IN A      <that node's public ip>
*.exm-001.usercontent.example.com.  IN CNAME  exm-001.usercontent.example.com.
```

plus an AAAA at the apex for an az that sets `public_ipv6`. Preflight and
`roles/validate` check that these resolve publicly.

## Inventory variables

Set them in `group_vars/all/main.yml` and `secrets.yml`
(`inventories/example` shows them commented out):

| Variable | Meaning |
| --- | --- |
| `cs_lb_acme_dns_provider` | The lego DNS provider code, e.g. `cloudflare`, `route53`. Empty (the default) leaves load balancer certificates off. |
| `cs_lb_acme_dns_env` | Vaulted dictionary of that provider's environment variables, e.g. `CLOUDFLARE_DNS_API_TOKEN: "CHANGEME"`. Required when a provider is set. |
| `cs_lb_acme_dns_alias_zone` | Optional. A zone at the provider that holds the challenge records instead of the load balancer's own zone. See below. |

The seed writes them to the controller as `le_dns_provider`,
`le_dns_provider_secret` (the env dictionary, one `KEY=value` per line) and
`le_dns_alias_zone`. With a provider set, every new load balancer is created
with `le: true`. Preflight rejects a malformed provider code, an empty or
malformed env dictionary, and env or alias zone set without a provider; its
messages name keys, never values.

The seed writes these settings only while they are unconfigured on the
controller. Changing or rotating the credential later is done in the
controller admin, not by editing the inventory; `roles/validate` warns when
the inventory and the controller differ.

These variables are independent of `acme_challenge_method` and the acme.sh
settings used for the portal, metrics and registry certificates
([acme-providers.md](acme-providers.md)): the zones and credentials may differ.

## Direct to the zone (normal case)

Give the provider a credential that can write TXT records in the zone that
holds the load balancer's domain, and set only the provider and env:

```yaml
# group_vars/all/main.yml
cs_lb_acme_dns_provider: cloudflare

# group_vars/all/secrets.yml (vaulted)
cs_lb_acme_dns_env:
  CLOUDFLARE_DNS_API_TOKEN: "CHANGEME"
```

The controller writes `_acme-challenge.<app_domain>` itself. If a CNAME already
exists at that name (for example from an earlier alias setup), remove it:
`roles/validate` warns about it.

## With an alias zone (optional)

If the credential must not touch the zone the load balancer domains live in,
set `cs_lb_acme_dns_alias_zone: acme.example.com`, a zone at the provider the
credential can write. The controller then writes its challenge records in that
zone, and each load balancer needs one permanent CNAME in the real zone:

```
_acme-challenge.exm-001.usercontent.example.com.  IN CNAME  lb<LB id>.acme.example.com.
```

`<LB id>` is the load balancer's id on the controller. It is assigned by the
seed, so create this record after the seed has run; the end-of-run "Remaining by
hand" output lists the record for each az the run added, and `roles/validate`
warns about a missing or wrong one.

## Upgrading an existing environment to controller 9.9

The controller no longer validates load balancer certificates through its own
PowerDNS, so **no load balancer certificate is issued or renewed until
`le_dns_provider` is set.** Existing certificates keep serving until they
expire. In this order:

1. If you use an alias zone, create the `_acme-challenge` CNAMEs above.
2. Set the inventory variables and re-run `site.yml`, which seeds them while
   the controller has them unconfigured; or set the provider, credentials and
   alias zone in the controller admin.
3. Clear or replace any old `le_dns_alias_zone` left from the PowerDNS setup.
   Its meaning changed: it is now a zone at your DNS provider.
4. The seed does not change existing load balancer rows, and load balancers
   this provisioner created earlier have `le` off. Turn on `le` for each one in
   the controller admin; saving it orders the certificate. The rake task
   `lets_encrypt:issue_lb[<id>|all]` only re-triggers issuance for load
   balancers that already have `le` on; it skips the others.

`roles/validate` (check `lb_acme`) warns on a blank provider, a provider the
inventory sets but the controller does not have, missing credentials, and a
wrong or leftover `_acme-challenge` CNAME, and on a controller
`le_dns_alias_zone` the inventory does not set (possibly a pre-9.9 PowerDNS
zone). In attach mode the warnings point at the controller admin, never at
the inventory. It never fails the run: the
self-signed certificate keeps the load balancer serving meanwhile.
