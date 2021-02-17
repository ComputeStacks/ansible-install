#!/usr/bin/env bash

for c in $(docker ps -q --filter "ancestor=cmptstks/registry:latest" --filter "ancestor=registry:2") ; do
  docker restart $c
done
