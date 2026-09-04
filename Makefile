# Thin wrapper around the same commands README.md and docs/install.md show.
# It saves typing; it does not hide anything. Every playbook target needs an
# inventory and there is no default one -- ENV names a directory under
# inventories/, and running against the wrong environment is not a mistake a
# convenience Makefile gets to make for you.
#
#   make deps
#   make site ENV=prod
#   make add-region ENV=prod
#   make validate ENV=prod
#   make lint check
#
# ARGS passes anything else straight through:
#
#   make site ENV=prod ARGS='--check --diff'
#   make site ENV=prod ARGS='--vault-password-file ~/.vault-prod'

ANSIBLE_PLAYBOOK  ?= ansible-playbook
ANSIBLE_INVENTORY ?= ansible-inventory
ANSIBLE_GALAXY    ?= ansible-galaxy
ANSIBLE_LINT      ?= ansible-lint

# Vaulted secrets: prompt by default, override for a password file or a
# no-vault inventory (VAULT= on the command line).
VAULT ?= --ask-vault-pass
ARGS  ?=

# LIMIT is passed straight through as --limit. READ THIS BEFORE USING IT with
# a region_*/az_* pattern.
#
# Those groups are constructed from node host vars
# (inventories/<env>/zz_constructed.yml), so they contain NODES AND NOTHING
# ELSE. `--limit region_exm003` therefore removes the controller, the metrics
# host and the backup server from the run entirely, and every play that
# targets one of them runs against zero hosts. Ansible does not treat that as
# an error; it prints "skipping: no hosts matched" and carries on to the next
# play, so the run ends green having done roughly half the work:
#
#   site.yml         no controller_seed (no Location/Region/Node rows, so the
#                    node's cs-agent has nothing to enrol against), no
#                    prometheus file_sd fragments, no firewall on the shared
#                    hosts, no ssh trust from the controller.
#   add-region.yml   the same, plus no controller prep and none of the v1
#                    firewall appends -- the new node is built and then
#                    reachable by nobody.
#
# So: use a region limit to re-run node-only work on an already-converged
# environment, and add back what the run needs when you need more than that
# --- `LIMIT='region_exm003:controller:metrics:backup'`. add-region.yml needs
# no limit at all: its inventory already describes one new region.
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

.PHONY: help deps site add-region validate add-region-validate lint check

help:
	@echo "deps                              install the pinned galaxy roles and collections"
	@echo "site ENV=<name>                   greenfield converge (playbooks/site.yml)"
	@echo "add-region ENV=<name>             attach a region to an existing environment"
	@echo "validate ENV=<name>               re-run the post-install checks only"
	@echo "add-region-validate ENV=<name>    the same, for an attached region (add-region.yml)"
	@echo "lint                              ansible-lint, production profile"
	@echo "check                             syntax-check the playbooks and the role harness,"
	@echo "                                  then parse both inventories and render the site contract"
	@echo ""
	@echo "LIMIT=<pattern>, ARGS='...', VAULT='--vault-password-file ...' are honoured."
	@echo ""
	@echo "LIMIT=region_<name> selects that region's NODES only -- the plays that"
	@echo "target the controller, metrics and backup hosts then match no host and"
	@echo "are skipped, and the run still ends green. See the note in the Makefile."

# Paths match ansible.cfg's roles_path/collections_path; both are gitignored.
deps:
	$(ANSIBLE_GALAXY) role install -r requirements.yml -p galaxy_roles
	$(ANSIBLE_GALAXY) collection install -r requirements.yml -p collections

site:
	$(require_env)
	$(ANSIBLE_PLAYBOOK) $(PLAY_ARGS) playbooks/site.yml

# Attach mode. Do NOT reach for LIMIT here: this playbook already describes
# exactly one new region, and every other play in it targets a shared host
# that a region_* pattern would drop from the run (see the LIMIT note above,
# and docs/attach-mode.md).
add-region:
	$(require_env)
	$(ANSIBLE_PLAYBOOK) $(PLAY_ARGS) playbooks/add-region.yml

# Every check in roles/validate carries the `validate` tag (plus its own
# name), so this runs the assertions without re-running the converge.
# `make validate ENV=prod ARGS='--skip-tags borg'` narrows it further.
validate:
	$(require_env)
	$(ANSIBLE_PLAYBOOK) $(PLAY_ARGS) --tags validate playbooks/site.yml

# The attach-mode equivalent. site.yml is the wrong entry point for an
# attached region: its validate play covers hosts this repository did not
# build, while add-region.yml scopes the checks to the controller and the new
# node (docs/attach-mode.md).
add-region-validate:
	$(require_env)
	$(ANSIBLE_PLAYBOOK) $(PLAY_ARGS) --tags validate playbooks/add-region.yml

lint:
	$(ANSIBLE_LINT)

# Parses both playbooks and the per-role harness against the example
# inventory, then both shipped inventories -- no ENV, no connection, no
# secrets.
#
# The grep is a real regression test, not decoration. A directory inventory is
# parsed in ALPHABETICAL order, so the constructed plugin's source file has to
# sort AFTER the file that defines the hosts or it matches nothing and creates
# no groups -- silently, because a plugin that produces nothing is not an
# error. That is why it is named zz_constructed.yml, and every `--limit
# region_*` in this repository depends on it.
check:
	$(ANSIBLE_PLAYBOOK) --syntax-check -i inventories/example playbooks/site.yml
	$(ANSIBLE_PLAYBOOK) --syntax-check -i inventories/example playbooks/add-region.yml
	$(ANSIBLE_PLAYBOOK) --syntax-check -i inventories/example tests/roles.yml
	$(ANSIBLE_PLAYBOOK) --syntax-check -i tests/fixtures/single-site playbooks/site.yml
	@$(ANSIBLE_INVENTORY) -i inventories/example --graph | grep -q '@region_' || { \
	  echo "inventories/example produced no region_* groups -- the constructed"; \
	  echo "plugin source must sort AFTER hosts.yml (zz_constructed.yml)."; \
	  exit 1; }
	@$(ANSIBLE_INVENTORY) -i tests/fixtures/single-site --graph | grep -q '@region_' || { \
	  echo "tests/fixtures/single-site produced no region_* groups."; exit 1; }
	$(ANSIBLE_PLAYBOOK) -c local -i inventories/example tests/site_contract.yml
	$(ANSIBLE_PLAYBOOK) -c local -i tests/fixtures/single-site tests/site_contract.yml
	@echo "check: inventories parse, region groups exist, site contract resolves"
