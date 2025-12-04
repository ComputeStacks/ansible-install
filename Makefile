.PHONY: help roles

help: ## Help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help

roles: ## Install ansible dependencies
	@time --format="took %E" ansible-galaxy install -r requirements.yml --force

bootstrap: ## Bootstrap a new cluster, including add new availability zones.
	@time --format="took %E" ansible-playbook -u root -i inventory.yml main.yml --tags bootstrap

nameservers: ## Runs the base package installation on ALL servers, but stops after nameservers are provisioned
	@time --format="took %E" ansible-playbook -u root -i inventory.yml main.yml --tags nameserver

validate: ## Run validation checks on an installation to check for common issues
	@time --format="took %E" ansible-playbook -u root -i inventory.yml main.yml --tags validate

vault-unseal: ## Unseal vault on the controller
	@time --format="took %E" ansible-playbook -u root -i inventory.yml main.yml --tags vault_unseal

