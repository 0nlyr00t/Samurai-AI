#!/usr/bin/env bash
# Build the standalone `samurai` binary with PyInstaller.
set -euo pipefail

cd "$(dirname "$0")/.."
uv run --group release pyinstaller packaging/samurai.spec \
    --distpath dist/binary --workpath build/pyinstaller --noconfirm "$@"
echo "binary: dist/binary/samurai"
