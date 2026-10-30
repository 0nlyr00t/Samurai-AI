#!/usr/bin/env bash
# Samurai AI installer.
#
#   curl -fsSL https://raw.githubusercontent.com/0nlyr00t/Samurai-AI/main/install.sh | bash
#
# Installs the `samurai` CLI with uv (https://docs.astral.sh/uv/), which also
# provides the required Python 3.12+ toolchain. To install a specific release:
#
#   curl -fsSL .../install.sh | bash -s -- --ref v0.3.0
#
set -euo pipefail

REPO="https://github.com/0nlyr00t/Samurai-AI.git"
REF=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      REF="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
done

info() { printf '\033[0;36m==>\033[0m %s\n' "$*"; }

# uv runs Python 3.12 for the tool venv even on hosts without it.
if ! command -v uv >/dev/null 2>&1; then
  info "uv not found — installing via astral.sh"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ -z "$REF" ]; then
  # Latest release tag (works for private repos via the git credential helper).
  REF=$(git ls-remote --tags "$REPO" 2>/dev/null \
    | grep -oE 'refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sed 's|refs/tags/||' \
    | sort -t. -k1,1n -k2,2n -k3,3n \
    | tail -1 || true)
fi
if [ -z "$REF" ]; then
  REF="main"
  info "no release tags found — installing from the main branch"
else
  info "installing Samurai AI $REF"
fi

uv tool install --force --refresh "git+${REPO}@${REF}"

info "done. If 'samurai' is not on your PATH yet, add ~/.local/bin to it:"
echo '  export PATH="$HOME/.local/bin:$PATH"'
echo
samurai --version 2>/dev/null || true
