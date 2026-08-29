# node_exporter

**Owner: Wave 4J.** `hosts: all`.

## Purpose

Installs the distro `prometheus-node-exporter` package, holds it, and keeps it
running. Nothing else.

This role exists because nothing else owned it. The plan says node_exporter
"stays the distro apt package" and is "applied via a community role in the
all-hosts play", but no such role was ever pinned in `requirements.yml`, and
v1's own `roles/node_exporter` was a two-task `apt: state=latest` + `systemd`
pair. Both consumers were already built and waiting for it:

* `roles/metrics` scrapes `cs_ports.node_exporter` on every node (per-AZ
  `file_sd` fragments) **and** on the infrastructure groups
  (`metrics_infra_node_exporter_groups`: controller, registry, metrics,
  backup, nameservers) as static targets;
* `roles/firewall` opens that port for the metrics host's addresses on every
  host in the fleet.

A three-task role is cheaper than a play-level task block duplicated across
`site.yml` and `add-region.yml`, and it is lintable and taggable like every
other role here.

## Bind address

The package binds `:9100` on all interfaces, and that is deliberate: the
scrape address is a property of the *metrics host*, not of the target
(docs/contracts.md §Tailscale address derivation — prometheus dials a node's
tailnet address whenever the metrics host is tailnet-joined, and its
`primary_ip` otherwise). A single-address bind would refuse one of those two
paths. Access control is `roles/firewall`'s input chain, exactly as for
cadvisor (`roles/node_observability/README.md` documents the same reasoning at
length).

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `node_exporter_package` | `prometheus-node-exporter` | Distro package name. |
| `node_exporter_service` | `prometheus-node-exporter` | Unit name. |
| `node_exporter_apt_version` | `""` (`versions.yml`) | Empty installs whatever the archive offers; set an exact apt version to pin. See the VERIFY note in `versions.yml`. |
| `node_exporter_hold_package` | `true` | `apt-mark hold` equivalent. Released before each install, so a version change still rolls through on a rerun. |

## Deviations from v1 (`roles/node_exporter`)

* `state: present` + hold instead of `state: latest` unheld, which let an
  unattended upgrade swap the exporter under a live scrape.
* The hold is released before the install, so the role stays convergent when
  the pinned version moves (docs/contracts.md rule 3).
