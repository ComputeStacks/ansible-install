# node_exporter

Installs the distro `prometheus-node-exporter` package, holds it, and keeps it
running on every host. Nothing else.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Base system" | `all` |
| `add-region.yml` | "New node base system" | `nodes` (the new node) |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** new node only; existing hosts keep the exporter they have.
**Requires:** —

## Purpose

Its consumers:

* `roles/metrics` scrapes `cs_ports.node_exporter` on every node (per-AZ
  `file_sd` fragments) **and** on the infrastructure groups
  (`metrics_infra_node_exporter_groups`: controller, registry, metrics,
  backup, nameservers) as static targets;
* `roles/firewall` opens that port for the metrics host's addresses on every
  host in the fleet.

A small role rather than a play-level task block duplicated across
`site.yml` and `add-region.yml`, so it is lintable and taggable like every
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
| `node_exporter_apt_version` | `versions.yml` | Exact apt version. Empty installs whatever the archive offers. |
| `node_exporter_hold_package` | `true` | `apt-mark hold` equivalent. Released before each install, so a version change still rolls through on a rerun. |

The package is installed at an exact version and held, so an unattended
upgrade cannot swap the exporter under a live scrape; releasing the hold
before each install keeps the role convergent when the pin moves
(docs/contracts.md rule 3).
