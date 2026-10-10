# Agent instructions

Guidance for AI coding agents working in this repository. Humans: the same
rules are in [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/contracts.md](docs/contracts.md).

## What this is

Ansible playbooks that install and maintain a ComputeStacks environment on
Ubuntu 26.04 hosts. Entry points: `playbooks/site.yml` (greenfield),
`playbooks/add-region.yml` (attach a region to an existing environment),
`playbooks/upgrade-docker.yml` (rolling docker upgrade of the nodes). Read
[docs/contracts.md](docs/contracts.md) before changing a role — code comments
cite its rules by number ("contracts rule 3") and section ("§Variable scope").

## Never

* **Never run a playbook, role or ad-hoc module against the machine you are
  running on**, and never against any host you were not explicitly given.
  These playbooks reconfigure firewalls, install docker, reboot hosts, and
  `roles/node_storage` formats disks. The only things safe to run locally
  are `make check` and `make lint`: they parse and render, and the few
  `-c local` test plays touch no device and no service.
* **Never commit a real hostname, IP address, domain or credential.** This
  is a public repository. Use RFC 5737 / RFC 1918 addresses and
  `example.com` names (contracts rule 6). Every directory under
  `inventories/` except `example/` is gitignored; real inventories live
  there. `workspace/` is gitignored scratch.
* **Never generate or rotate `secret_key_base` / `user_auth_secret`** in code
  or docs (contracts rule 8).

## Working in the tree

* **A fresh checkout or git worktree has no galaxy dependencies.** Run
  `make deps`. Don't symlink `galaxy_roles/` or `collections/` from another
  checkout: the links break when ansible runs inside a container that only
  mounts this directory.
* **Verify with `make check` and `make lint`** (production profile) before
  calling a change done. Pure logic lives in filter plugins with unit tests
  under `tests/` (see `tests/node_storage_classify.yml`); add a case there
  for new logic rather than relying on a live run.
* **Version pins** live only in `playbooks/group_vars/all/versions.yml`, each
  with the date and source it was verified against. After a bump, run
  `make check-pins` (network; checks every pin exists upstream).
* **Templates read inventory vars only**, never gathered facts, and never
  index `[0]` (contracts rules 1 and 2). Ports come from `cs_ports`
  (rule 5).
* **Behavior only a real host can show** — disks, reboots, firewalls, attach
  mode — needs an end-to-end run on throwaway Ubuntu 26.04 VMs, done by or
  with the explicit go-ahead of the person you are working for.

## Writing

* American English.
* Each role's `README.md` opens with the same header table (Hosts, Runs in,
  Attach mode, Requires), then what it does, its variables and design notes.
  Describe the role as it is; don't narrate how it was built.
* Operator-visible changes get a dated entry at the top of `CHANGELOG.md`.
* Commit messages: short, imperative, no trailers.
