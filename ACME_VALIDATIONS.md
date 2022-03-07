# Acme Validation Methods

By default, acme validation for the controller and metrics server will be done via HTTP. To support other configurations, this ansible installer can also validate via DNS integrations.

To switch from `http` validation to your preferred dns provider, add the following to your `inventory.yml` file.

## AutoDNS

```yaml
acme_challenge_method: "autodns"
acme_autodns_user: ""
acme_autodns_pass: ""
acme_autodns_context: ""
```

## AWS Route 53

[https://github.com/Neilpang/acme.sh/wiki/How-to-use-Amazon-Route53-API](https://github.com/Neilpang/acme.sh/wiki/How-to-use-Amazon-Route53-API)

```yaml
acme_challenge_method: "aws"
acme_aws_key: ""
acme_aws_secret: ""
```

## Cloudflare
```yaml
acme_challenge_method: "cloudflare"
acme_cf_token: "" # API Token
acme_cf_account: "" # Account ID
acme_cf_zone: "" # Zone ID (Optional)
```

## cPanel

```yaml
acme_challenge_method: "cpanel"
acme_cpanel_username: ""
acme_cpanel_api_key: ""
acme_cpanel_hostname: "" # https://cpanel.example.com:2083
```

## Digital Ocean

[https://www.digitalocean.com/help/api/](https://www.digitalocean.com/help/api/)

```yaml
acme_challenge_method: "do"
acme_do_key: ""
```

## Gandi

[https://api.gandi.net/docs/livedns/](https://api.gandi.net/docs/livedns/)

```yaml
acme_challenge_method: "gandi"
acme_gandi_key: ""
```

## GoDaddy

[https://developer.godaddy.com/keys/](https://developer.godaddy.com/keys/)

```yaml
acme_challenge_method: "godaddy"
acme_gd_key: ""
acme_gd_secret: ""
```

## IONOS

[https://developer.hosting.ionos.de/docs/getstarted](https://developer.hosting.ionos.de/docs/getstarted)

```yaml
acme_challenge_method: "ionos"
acme_ionos_prefix: ""
acme_ionos_secret: ""
```

## Linode

[https://cloud.linode.com/profile/tokens](https://cloud.linode.com/profile/tokens)

```yaml
acme_challenge_method: "linode"
acme_linode_token: ""
```

## NSUPDATE (RFC2136)

Instructions for configuring with [PowerDNS](https://doc.powerdns.com/authoritative/dnsupdate.html#per-zone-settings)

```yaml
acme_challenge_method: "nsupdate"
acme_dns_server: "ns1.example.com"
acme_dns_port: "53"
acme_dns_tsig_name: "test" # Name of your key
acme_dns_tsig_algo: "hmac-md5"
acme_dns_tsig_secret: ""
```

## PowerDNS

_Note: We recommend you use the NSUPDATE method, as that does not require granting complete server access._

[https://doc.powerdns.com/md/httpapi/README/](https://doc.powerdns.com/md/httpapi/README/)

```yaml
acme_challenge_method: "pdns"
acme_pdns_url: ""
acme_pdns_serverid: ""
acme_pdns_token: ""
```

## Vultr

Ensure the controller, registry, and metrics IPs are added to the api whitelist.

```yaml
acme_challenge_method: "vultr"
acme_vultr_key: ""
```