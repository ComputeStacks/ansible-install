# ACME validation methods

The `acme_web` role obtains the TLS certificate for the portal, metrics and
registry hosts with [acme.sh](https://github.com/acmesh-official/acme.sh),
pinned to the release tag in `playbooks/group_vars/all/versions.yml`
(`acme_sh_version`) and with acme.sh's own self-upgrade disabled.

**Default CA is ZeroSSL.** Set `use_zerossl: false` in the inventory for
Let's Encrypt.

**Default challenge is HTTP-01** over the webroot `/var/www/acme-sh`, served
by nginx on port 80. To use DNS-01 instead, set `acme_challenge_method` and
the provider's credentials as group/host vars in your inventory (put the
secrets in the vaulted `secrets.yml`, not in `main.yml`).

## Port 80 and the host firewall

HTTP-01 needs port 80 reachable from the CA. `roles/firewall` opens
`cs_ports.controller_http` on the **controller only** — the metrics and
registry hosts accept 3101/3102 and the tenant port range, not 80. Use a
DNS-01 provider on those hosts, or have the firewall wave open port 80 for
their groups. The role prints a warning when it detects this combination.

---

## AutoDNS

```yaml
acme_challenge_method: "autodns"
acme_autodns_user: ""
acme_autodns_pass: ""
acme_autodns_context: ""
```

## AWS Route 53

<https://github.com/acmesh-official/acme.sh/wiki/How-to-use-Amazon-Route53-API>

```yaml
acme_challenge_method: "aws"
acme_aws_key: ""
acme_aws_secret: ""
```

## Cloudflare

```yaml
acme_challenge_method: "cloudflare"
acme_cf_token: ""    # API token with write permission on the zone
acme_cf_account: ""  # Account ID
acme_cf_zone: ""     # Zone ID (optional)
```

## cPanel

```yaml
acme_challenge_method: "cpanel"
acme_cpanel_username: ""
acme_cpanel_api_key: ""
acme_cpanel_hostname: ""   # https://cpanel.example.com:2083
```

## DigitalOcean

<https://www.digitalocean.com/help/api/>

```yaml
acme_challenge_method: "do"
acme_do_key: ""
```

## Gandi

<https://api.gandi.net/docs/livedns/>

```yaml
acme_challenge_method: "gandi"
acme_gandi_key: ""
```

## GoDaddy

<https://developer.godaddy.com/keys/>

```yaml
acme_challenge_method: "godaddy"
acme_gd_key: ""
acme_gd_secret: ""
```

## IONOS

<https://developer.hosting.ionos.de/docs/getstarted>

```yaml
acme_challenge_method: "ionos"
acme_ionos_prefix: ""
acme_ionos_secret: ""
```

## Linode

<https://cloud.linode.com/profile/tokens>

```yaml
acme_challenge_method: "linode"
acme_linode_token: ""
```

Linode issuance runs with `--dnssleep 900`; expect the task to take a quarter
of an hour.

## NSUPDATE (RFC 2136)

The recommended option when this installation runs its own PowerDNS
nameservers — it grants update rights to one key on one zone rather than full
API access to the server. See
<https://doc.powerdns.com/authoritative/dnsupdate.html#per-zone-settings>.

```yaml
acme_challenge_method: "nsupdate"
acme_dns_server: "ns1.example.com"
acme_dns_port: "53"
acme_dns_tsig_name: "acme"         # name of your key
acme_dns_tsig_algo: "hmac-sha256"
acme_dns_tsig_secret: ""
```

The role writes the TSIG key to `/root/.acme-nsupdate.key`, mode 0600.

> **Changed from v1:** the default algorithm is `hmac-sha256`, not v1's
> `hmac-md5`. Set `acme_dns_tsig_algo: "hmac-md5"` explicitly if you are
> reusing an existing v1 key that was generated with it.

## PowerDNS API

Prefer NSUPDATE — this grants complete server access.
<https://doc.powerdns.com/authoritative/http-api/>

```yaml
acme_challenge_method: "pdns"
acme_pdns_url: "http://ns1.example.com:8081"
acme_pdns_serverid: "localhost"
acme_pdns_token: ""
acme_pdns_ttl: 60
```

## Vultr

Add the controller, registry and metrics IPs to the API allow list first.

```yaml
acme_challenge_method: "vultr"
acme_vultr_key: ""
```

---

## Adding a provider

`acme_web_dns_providers` in `roles/acme_web/defaults/main.yml` maps
`acme_challenge_method` to an acme.sh `--dns` plugin plus the environment that
plugin reads. Adding a provider is an entry in that map (and a section here) —
no new task file. v1 carried eleven near-identical task files under
`roles/nginx/tasks/dns/`; they are gone.
