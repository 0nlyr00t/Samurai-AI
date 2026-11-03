#!/usr/bin/env bash
# Build the ops toolbelt locally and print the digest to pin in the profile.
#
# The published path is CI (.github/workflows/ops-image.yml): it builds both
# architectures, pushes ghcr.io/<owner>/samurai-ops by digest and opens the pin
# PR. This script is for a local check before that — same platform caveat.
set -euo pipefail
cd "$(dirname "$0")"
platform="${PLATFORM:-linux/arm64}"
tag="${TAG:-samurai/ops:dev}"
docker buildx build --platform "$platform" -t "$tag" --load . >/dev/null
echo "local digest: $(docker image inspect "$tag" --format '{{.Id}}')"
echo "registry build: push this workflow's path and let CI publish the index digest"
