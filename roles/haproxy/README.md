# haproxy

**Owner:** Wave 2D. **Hosts:** `nodes`.

Installs distro haproxy on a node and pre-creates everything the controller's
runtime configuration depends on.

## The controller owns haproxy.cfg — this role does not

`/etc/haproxy/haproxy.cfg` is rendered **by the controller**, per load
balancer, and deployed **over SSH at runtime**
(`app/views/api/stacks/load_balancers/config.erb` in the controller repo). This
role must never write it: a config written here would be overwritten on the
next deploy, or worse, would look authoritative while being stale. What the
role does instead is make sure every path that generated config references
already exists:

| Path | Referenced by | Provided by |
|---|---|---|
| `/etc/haproxy/certs/` | `crt /etc/haproxy/certs/` | this role (0700) |
| `/etc/haproxy/default.http` | `errorfile 503` | this role (v1's page) |
| `/etc/haproxy/errors/<code>.http` | `errorfile 400/403/408/500/502/504` | distro package; this role backfills any missing one |
| `/etc/haproxy/dhparam.pem` | `ssl-dh-param-file` | this role |

The service is **enabled but not started**. The distro's stock `haproxy.cfg`
has no listener, so a node that has never received a controller config push has
nothing to serve; the controller starts/reloads haproxy when it deploys the
real config.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `haproxy_hold_package` | `true` | Hold the apt package so an unattended upgrade cannot swap the load balancer under a live region. |
| `haproxy_certs_dir` | `/etc/haproxy/certs` | Controller writes tenant certificates here. |
| `haproxy_errors_dir` | `/etc/haproxy/errors` | Error pages. |
| `haproxy_error_codes` | `[400, 403, 408, 500, 502, 504]` | Codes the controller's `defaults` section maps to `errors/<code>.http`. |
| `haproxy_manage_dhparam` | `true` | Generate `/etc/haproxy/dhparam.pem`. |
| `haproxy_dhparam_size` | `2048` | DH parameter size. |

The stats port the metrics host scrapes (`cs_ports.haproxy_stats`, 81) is set
by the controller in the generated config, not here — but it must equal
`load_balancer.stats_bind` on the controller side.

## Deviations from v1 (`roles/haproxy`)

- **Adds `/etc/haproxy/dhparam.pem`.** The controller's generated config carries
  `ssl-dh-param-file /etc/haproxy/dhparam.pem` unconditionally and haproxy
  refuses to start when the file is missing; nothing in v1 ever created it.
  Set `haproxy_manage_dhparam: false` if the file is supplied another way.
- **Adds `/etc/haproxy/errors/` and backfills missing error pages** with the
  generic 503 page (`force: false`, so a distro or operator page is never
  overwritten). v1 installed only `default.http`, leaving the six other
  `errorfile` paths entirely dependent on the distro package.
- **Holds the package** (v1 let unattended upgrades move it).
- **Enabled, not started** (v1 used `state: started`, which fails on a fresh
  node whose stock config has no listener).
- Not guarded by `when: ansible_facts.services[...] is not defined` — the role
  is convergent.
- `certs/` is 0700 (v1 left it at the default 0755 with private keys in it).
