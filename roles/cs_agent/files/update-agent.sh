#!/bin/bash

set -e

AGENT_TAR=$(mktemp)
curl -sSL --fail -o $AGENT_TAR https://f.cscdn.cc/file/cstackscdn/packages/cs-agent/cs-agent.tar.gz
tar -xzvf $AGENT_TAR --directory .
gpg --verify cs-agent.sig cs-agent || exit_code=$?
if [[ ${exit_code} -ne 0 ]]; then exit ${exit_code}; fi
systemctl stop cs-agent
mv cs-agent /usr/local/bin/
chown root:root /usr/local/bin/cs-agent && chmod +x /usr/local/bin/cs-agent
rm $AGENT_TAR
rm cs-agent.sig
systemctl start cs-agent
