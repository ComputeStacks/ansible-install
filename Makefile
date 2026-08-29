# Thin wrapper around the same commands README.md and docs/install.md show.
# It saves typing; it does not hide anything. Every playbook target needs an
# inventory and there is no default one -- ENV names a directory under
# inventories/, and running against the wrong environment is not a mistake a
# convenience Makefile gets to make for you.
#
#   make deps
#   make site ENV=prod
#   make add-region ENV=prod LIMIT=region_exm003
#   make validate ENV=prod
#   make lint check
#
# ARGS passes anything else straight through:
#
#   make site ENV=prod ARGS='--check --diff'
#   make site ENV=prod ARGS='--vault-password-file ~/.vault-prod'

ANSIBLE_PLAYBOOK ?= ansible-playbook
ANSIBLE_GALAXY   ?= ansible-galaxy
ANSIBLE_LINT     ?= ansible-lint

# Vaulted secrets: prompt by default, override for a password file or a
# no-vault inventory (VAULT= on the command line).
VAULT ?= --ask-vault-pass
ARGS  ?=
LIMIT ?=

INVENTORY = inventories/$(ENV)
LIMIT_ARG = $(if $(LIMIT),--limit $(LIMIT),)
PLAY_ARGS = -i $(INVENTORY) $(VAULT) $(LIMIT_ARG) $(ARGS)

define require_env
	@test -n "$(ENV)" || { \
	  echo "ENV is required: make $@ ENV=<inventory name> (a directory under inventories/)"; \
	  exit 1; }
	@test -d "$(INVENTORY)" || { \
	  echo "No inventory at $(INVENTORY) -- copy inventories/example and edit it."; \
	  exit 1; }
endef

.PHONY: help deps site add-region validate lint check

help:
	@echo "deps                              install the pinned galaxy roles and collections"
	@echo "site ENV=<name>                   greenfield converge (playbooks/site.yml)"
	@echo "add-region ENV=<name>             attach a region to an existing environment"
	@echo "validate ENV=<name>               re-run the post-install checks only"
	@echo "lint                              ansible-lint, production profile"
	@echo "check                             syntax-check both playbooks and the role harness"
	@echo ""
	@echo "LIMIT=<pattern>, ARGS='...', VAULT='--vault-password-file ...' are honoured."

# Paths match ansible.cfg's roles_path/collections_path; both are gitignored.
deps:
	$(ANSIBLE_GALAXY) role install -r requirements.yml -p galaxy_roles
	$(ANSIBLE_GALAXY) collection install -r requirements.yml -p collections

site:
	$(require_env)
	$(ANSIBLE_PLAYBOOK) $(PLAY_ARGS) playbooks/site.yml

# Attach mode. LIMIT is not enforced here, but the new region's node is what
# this playbook is meant to be scoped to -- see docs/attach-mode.md.
add-region:
	$(require_env)
	$(ANSIBLE_PLAYBOOK) $(PLAY_ARGS) playbooks/add-region.yml

# Every check in roles/validate carries the `validate` tag (plus its own
# name), so this runs the assertions without re-running the converge.
# `make validate ENV=prod ARGS='--skip-tags borg'` narrows it further.
validate:
	$(require_env)
	$(ANSIBLE_PLAYBOOK) $(PLAY_ARGS) --tags validate playbooks/site.yml

lint:
	$(ANSIBLE_LINT)

# Parses both playbooks and the per-role harness against the example
# inventory -- no ENV, no connection, no secrets.
check:
	$(ANSIBLE_PLAYBOOK) --syntax-check -i inventories/example playbooks/site.yml
	$(ANSIBLE_PLAYBOOK) --syntax-check -i inventories/example playbooks/add-region.yml
	$(ANSIBLE_PLAYBOOK) --syntax-check -i inventories/example tests/roles.yml
