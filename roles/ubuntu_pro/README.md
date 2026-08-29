# ubuntu_pro

**Owner:** Wave 2F (with `firewall` and `tailscale`)

Attaches a host to an Ubuntu Pro contract and keeps livepatch enabled. Runs on
`hosts: all`, gated in `site.yml` on `ubuntu_pro_token` being set in the vaulted
secrets file. Entirely optional — an environment without a token never runs it.

## What it does

1. Installs `ubuntu-pro-client` (present on Ubuntu server images already; the
   task keeps the role convergent on a minimal image).
2. Asks `pro api u.pro.status.is_attached.v1` whether the host is attached, and
   runs `pro attach` **only if it is not**. `pro attach` on an already-attached
   host exits non-zero, so the check is what makes the role idempotent.
3. Reads `pro status --all --format json`, diffs the enabled services against
   `ubuntu_pro_services`, and enables only what is missing.

The token is passed as a command argument, so the attach task is `no_log: true`
— neither the command line nor its output ever reaches the log, at any verbosity.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `ubuntu_pro_package` | `ubuntu-pro-client` | Client package. |
| `ubuntu_pro_bin` | `/usr/bin/pro` | Client binary. |
| `ubuntu_pro_services` | `[livepatch]` | Services asserted enabled after attach. |
| `ubuntu_pro_attach_args` | `""` | Extra `pro attach` flags (e.g. `--no-auto-enable`). |

Consumed, not owned: `ubuntu_pro_token` (vaulted secret).

## Notes

- `pro attach` auto-enables the entitlements the contract ships with (on an LTS
  that includes `esm-infra`, `esm-apps` and `livepatch`); the explicit
  `ubuntu_pro_services` pass is what guarantees livepatch specifically, and what
  re-enables it if someone disabled it by hand. Add `--no-auto-enable` to
  `ubuntu_pro_attach_args` to opt out of everything else.
- `pro api u.pro.status.is_attached.v1` needs ubuntu-advantage-tools >= 27.11.
  Anything older fails the task loudly rather than silently re-attaching.
- Enabling a service the kernel/contract does not support (livepatch on a
  non-supported kernel) fails loudly by design — it is a real misconfiguration,
  not something to skip past.
- Detaching is deliberately not implemented: removing the token from secrets
  simply stops the role from running. `pro detach` on a live fleet is an
  operator decision, not a converge side effect.
