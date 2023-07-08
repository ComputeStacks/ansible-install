.PHONY: help

help: ## Help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help

roles: ## Install ansible dependencies
	@ansible-galaxy install -r requirements.yml

bootstrap: ## Bootstrap a new cluster, including add new availability zones.
	@time ansible-playbook -u root -i inventory.yml main.yml --tags bootstrap

validate: ## Run validation checks on an installation to check for common issues
	@time ansible-playbook -u root -i inventory.yml main.yml --tags validate

vault-unseal: ## Unseal vault on the controller
	@time ansible-playbook -u root -i inventory.yml main.yml --tags vault_unseal
