#!/usr/bin/env bash
# Verify that the sdist/wheel build is bit-reproducible under a fixed
# SOURCE_DATE_EPOCH. Two consecutive builds must yield identical sha256.
set -euo pipefail

cd "$(dirname "$0")/.."
export SOURCE_DATE_EPOCH=1700000000

build_into() {
    local target="$1"
    rm -rf "$target"
    uv build --out-dir "$target" >/dev/null
}

build_into dist/repro-a
build_into dist/repro-b

fail=0
if command -v sha256sum >/dev/null; then
    sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
else
    sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi
for name in $(ls dist/repro-a); do
    hash_a=$(sha256_of "dist/repro-a/$name")
    hash_b=$(sha256_of "dist/repro-b/$name")
    if [ "$hash_a" != "$hash_b" ]; then
        echo "MISMATCH: $name"
        echo "  a: $hash_a"
        echo "  b: $hash_b"
        fail=1
    else
        echo "OK: $name $hash_a"
    fi
done
exit "$fail"
