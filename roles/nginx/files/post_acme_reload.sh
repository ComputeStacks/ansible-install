#!/usr/bin/env bash

for c in $(docker ps -q --filter "ancestor=cmptstks/registry:latest" --filter "ancestor=registry:2") ; do
  docker restart $c
done

if [ -d /etc/nginx/ssl/acme ]; then
  rm -rf /etc/nginx/ssl/acme/*
  rsync -aP /opt/container_registry/ssl/ /etc/nginx/ssl/acme/
  systemctl restart nginx
fi
