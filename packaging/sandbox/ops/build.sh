#!/usr/bin/env bash
# Build the ops toolbelt and print the digest to pin in the profile.
set -euo pipefail
cd "$(dirname "$0")"
docker build --platform linux/arm64 -t samurai/ops:dev . >/dev/null
docker image inspect samurai/ops:dev --format '{{index .RepoDigests 0}}' || true
docker image inspect samurai/ops:dev --format 'local digest: {{.Id}}'
echo "pin: $(docker image inspect samurai/ops:dev --format '{{.Id}}' | sed 's/sha256:/sha256:/')"
