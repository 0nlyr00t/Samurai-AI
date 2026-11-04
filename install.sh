#!/usr/bin/env bash
# Samurai AI installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/0nlyr00t/Samurai-AI/main/install.sh | bash
#
# Installs the signed release binary — no Python, no uv, no sudo. Options:
#
#   --ref <tag>        install a specific release (default: the newest tag)
#   --force-uv         skip the binary and install from the tag via uv
#   --dry-run          print the plan, change nothing
#
# Environment: SAMURAI_INSTALL_DIR (default ~/.local/bin), GH_TOKEN for the
# release API when the repository is private.

set -euo pipefail

REPO_SLUG="0nlyr00t/Samurai-AI"
REPO="https://github.com/${REPO_SLUG}.git"
API="https://api.github.com/repos/${REPO_SLUG}"
RAW="https://raw.githubusercontent.com/${REPO_SLUG}"
INSTALL_DIR="${SAMURAI_INSTALL_DIR:-$HOME/.local/bin}"
MARK_BEGIN="# >>> samurai >>>"
MARK_END="# <<< samurai <<<"
REF=""
FORCE_UV=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --force-uv) FORCE_UV=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

info() { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m warn\033[0m %s\n' "$*" >&2; }
die() { printf '\033[0;31merror\033[0m %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "    + $*"; else "$@"; fi; }

# -- platform ------------------------------------------------------------
case "$(uname -s)" in
  Darwin) os="darwin" ;;
  Linux) os="linux" ;;
  *) die "only macOS and Linux are supported by this script; on Windows install the .exe from the release" ;;
esac
case "$(uname -m)" in
  arm64|aarch64) arch="arm64" ;;
  x86_64|amd64) arch="x86_64" ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
ASSET="samurai-${os}-${arch}"

# -- release -------------------------------------------------------------
if [ -z "$REF" ]; then
  REF=$(curl -fsSL ${GH_TOKEN:+-H "Authorization: Bearer $GH_TOKEN"} \
    "${API}/releases/latest" 2>/dev/null \
    | grep -oE '"tag_name": *"[^"]+"' | head -1 | sed 's/.*"\(v[^"]*\)"$/\1/' || true)
fi
if [ -z "$REF" ]; then
  REF=$(git ls-remote --tags "$REPO" 2>/dev/null \
    | grep -oE 'refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sed 's|refs/tags/||' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1 || true)
fi
if [ -z "$REF" ]; then
  REF="main"
  info "no release tags found — installing from the main branch"
else
  info "installing Samurai AI $REF"
fi

# -- legacy install ------------------------------------------------------
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^samurai\b'; then
  info "an older uv-based install is present — removing it first"
  run uv tool uninstall samurai >/dev/null 2>&1 || true
fi

add_to_path_profile() {
  case "$(basename "${SHELL:-sh}")" in
    zsh) profile="$HOME/.zshrc" ;;
    bash) profile="$HOME/.bashrc" ;;
    *) profile="$HOME/.profile" ;;
  esac
  case ":$PATH:" in
    *":$INSTALL_DIR:"*) return 0 ;;
  esac
  if [ -f "$profile" ] && grep -qF "$MARK_BEGIN" "$profile"; then
    return 0
  fi
  info "adding $INSTALL_DIR to PATH in $profile"
  if [ "$DRY_RUN" = 1 ]; then
    echo "    + append PATH block to $profile"
    return 0
  fi
  mkdir -p "$(dirname "$profile")"
  {
    printf '\n%s\n' "$MARK_BEGIN"
    printf 'export PATH="%s:$PATH"\n' "$INSTALL_DIR"
    printf '%s\n' "$MARK_END"
  } >>"$profile"
}

# -- binary route --------------------------------------------------------
install_binary() {
  local base="${RAW}/${REF}"
  local tmp
  tmp=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" EXIT
  info "downloading $ASSET ($REF)"
  # 3 = this platform has no asset in this release; anything worse is fatal
  curl -fsSL --retry 3 -o "${tmp}/${ASSET}" "${base}/${ASSET}" || return 3
  curl -fsSL --retry 3 -o "${tmp}/${ASSET}.sha256" "${base}/${ASSET}.sha256" \
    || die "the release has no checksum for $ASSET"

  ( cd "$tmp" && sha256sum -c "${ASSET}.sha256" >/dev/null 2>&1 \
      || shasum -a 256 -c "${ASSET}.sha256" >/dev/null 2>&1 ) \
    || die "checksum mismatch for $ASSET — refusing to install"

  if [ "${SKIP_COSIGN:-0}" != 1 ]; then
    if command -v cosign >/dev/null 2>&1; then
      if curl -fsSL --retry 3 -o "${tmp}/${ASSET}.bundle" "${base}/${ASSET}.bundle" 2>/dev/null; then
        info "verifying the Sigstore signature"
        cosign verify-blob --bundle "${tmp}/${ASSET}.bundle" "${tmp}/${ASSET}" \
          --certificate-oidc-issuer https://token.actions.githubusercontent.com \
          --certificate-identity-regexp "https://github.com/${REPO_SLUG}/.github/workflows/release.yml" \
          >/dev/null 2>&1 || die "signature check failed for $ASSET"
      else
        warn "no signature bundle in the release — checksum only"
      fi
    else
      warn "cosign is not installed — checksum verified, signature not checked"
    fi
  fi

  mkdir -p "$INSTALL_DIR"
  run install -m 0755 "${tmp}/${ASSET}" "${INSTALL_DIR}/samurai"
}

install_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    info "uv not found — installing it from astral.sh"
    run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    export PATH="$HOME/.local/bin:$PATH"
  fi
  info "installing from ${REPO}@${REF} via uv"
  run uv tool install --force --refresh "git+${REPO}@${REF}"
}

if [ "$FORCE_UV" = 1 ]; then
  info "binary route skipped by --force-uv"
  install_uv
else
  status=0
  install_binary || status=$?
  case "$status" in
    0) ;;
    3)
      warn "no ${ASSET} in release ${REF} — falling back to uv for this platform"
      install_uv
      ;;
    *) exit "$status" ;;
  esac
fi

add_to_path_profile

echo
if [ "$DRY_RUN" = 1 ]; then
  info "dry run — nothing was installed"
  exit 0
fi
info "installed. start the app with: samurai"
if ! command -v samurai >/dev/null 2>&1; then
  echo "  $INSTALL_DIR is not on PATH in this shell yet:"
  echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
fi
if [ "$DRY_RUN" = 0 ]; then
  "$INSTALL_DIR/samurai" --version 2>/dev/null || samurai --version 2>/dev/null || true
fi
