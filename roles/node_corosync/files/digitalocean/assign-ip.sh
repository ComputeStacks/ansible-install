#!/usr/bin/env bash
#
# Source: https://gist.github.com/andrewsomething/75a1d6acd1155013fabd#gistcomment-2855333
#
API_BASE="https://api.digitalocean.com/v2"

if [[ -z "${DO_TOKEN}" ]] || [[ $# != 2 ]]; then
    echo "Usage: $0 <Floating IP> <Droplet ID>"
    echo "Your DigitalOcean API token must be in the \"DO_TOKEN\" environmental variable."
    exit 1
fi

curl -X POST -H "Content-Type: application/json" -H "Authorization: Bearer ${DO_TOKEN}" -d "{\"type\":\"assign\",\"droplet_id\":$2}" "${API_BASE}/floating_ips/$1/actions"
