#!/usr/bin/env bash
# Build a belt image locally and print the digest to pin in the profile.
#
# The published path is CI (.github/workflows/ops-image.yml): it builds both
# architectures, pushes the image by digest and opens the pin PR. This script is
# the local check before that — same platform caveat.
#
#   ./build.sh remote            # or: BELT=exploit ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
belt="${1:-${BELT:-remote}}"
platform="${PLATFORM:-linux/arm64}"
tag="${TAG:-samurai/$belt:dev}"
docker buildx build --platform "$platform" -t "$tag" --load "$belt" >/dev/null
echo "local digest: $(docker image inspect "$tag" --format '{{.Id}}')"
echo "registry build: push this path and let ci publish the index digest"
