# Changelog

## July 6, 2023

**Significant Change: ComputeStacks has deprecated support for multi-node availability zones. This playbook now only installs a single node per-az.**

* Debian 12 Bookworm
* Added linux bridge networks for containers
* Removed calico
* Removed etcd
* Removed corosync and pacemaker
* Haproxy v2.8 is now used
* Full support for ipv6

## May 8, 2023

* Redis will now use the redis apt repo.
* Add script to ensure real IP is recovered on the controller when using Cloudflare.
* Various updates for v8.1.

## Apr 8, 2023

* Remove option to add ComputeStacks Support access. Can be manually added later.

## Apr 6, 2023

* Remove dnsmasq. No longer necessary.

## Mar 14, 2023

* Make SSH the default backup transport (previous was NFS).
