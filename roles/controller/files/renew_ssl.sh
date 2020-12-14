#!/usr/bin/env bash
#
# place here: /etc/cron.daily/cert_renew

function renew_certs
{
  echo "Renewing Certificates..."
  /usr/local/bin/cstacks certbot-renew
}

function load_certs
{
  echo "Loading updated Certificates..."
  let "count=0"
  for d in /etc/letsencrypt/live/* ; do
    if ! [ -f $d/fullchain.pem ]; then
      echo "Missing key for $d, skipping..."
    else
      cat $d/fullchain.pem $d/privkey.pem > /etc/haproxy/certs/lecert$count.pem
    fi
    ((count++))
  done
}

function reload_haproxy
{
  echo "Reloading HAProxy..."
  systemctl reload haproxy
}

function reload_container_registry
{
  echo "Reloading container registires"
  for c in $(docker ps -q --filter "ancestor=cmptstks/registry:latest" --filter "ancestor=registry:2") ; do
    docker restart $c
  done
}

renew_certs
load_certs
reload_haproxy
reload_container_registry
exit 0

