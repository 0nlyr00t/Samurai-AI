#!/usr/bin/env bash
# Samurai AI uninstaller for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/0nlyr00t/Samurai-AI/main/uninstall.sh | bash
#
# Removes what the installer put down: the binary, a uv-based install, the PATH
# block it wrote, the state directory and the two belt images. Options:
#
#   --yes         do not ask (for `curl | bash`)
#   --keep-state  keep ~/.config/samurai (database, vault key, settings)
#   --dry-run     print what would go, change nothing
#
# Environment: SAMURAI_INSTALL_DIR (default ~/.local/bin), SAMURAI_HOME.

set -euo pipefail

INSTALL_DIR="${SAMURAI_INSTALL_DIR:-$HOME/.local/bin}"
STATE_DIR="${SAMURAI_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/samurai}"
MARK_BEGIN="# >>> samurai >>>"
MARK_END="# <<< samurai <<<"
BELT_IMAGES="ghcr.io/0nlyr00t/samurai-remote ghcr.io/0nlyr00t/samurai-exploit"
ASSUME_YES=0
KEEP_STATE=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    --keep-state) KEEP_STATE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

info() { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
gone() { printf '    removed %s\n' "$*"; }
kept() { printf '    kept    %s\n' "$*"; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "    + $*"; else "$@"; fi; }

# What is actually here?
found=0
has_block=0
for profile in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
  if [ -f "$profile" ] && grep -qF "$MARK_BEGIN" "$profile"; then
    has_block=1
    found=1
  fi
done
[ -e "$INSTALL_DIR/samurai" ] && found=1
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^samurai\b'; then
  found=1
fi
[ -d "$STATE_DIR" ] && found=1
# bash 3.2 (macOS) hates an empty array under `set -u`, so keep a plain string
images=""
for image in $BELT_IMAGES; do
  if command -v docker >/dev/null 2>&1 && docker image inspect "$image" >/dev/null 2>&1; then
    images="$images $image"
    found=1
  fi
done

if [ "$found" = 0 ]; then
  info "nothing to remove — Samurai AI does not look installed here"
  exit 0
fi

info "this will remove:"
[ -e "$INSTALL_DIR/samurai" ] && echo "    $INSTALL_DIR/samurai"
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^samurai\b'; then
  echo "    the uv tool install of samurai"
fi
[ "$has_block" = 1 ] && echo "    the PATH block in your shell profile"
if [ "$KEEP_STATE" = 1 ]; then
  echo "    (state stays: $STATE_DIR)"
else
  echo "    $STATE_DIR — database, vault key, settings"
fi
for image in $images; do echo "    docker image $image"; done

if [ "$ASSUME_YES" = 0 ]; then
  if [ -t 0 ]; then
    printf '\033[0;36m==>\033[0m continue? [y/N] '
    read -r answer || answer=""
    case "$answer" in
      y|Y|yes|YES) ;;
      *) info "left as it is"; exit 0 ;;
    esac
  else
    info "no terminal to ask — run with --yes to actually remove (dry run below)"
    DRY_RUN=1
  fi
fi

# 1. binary
if [ -e "$INSTALL_DIR/samurai" ]; then
  run rm -f "$INSTALL_DIR/samurai"
  [ "$DRY_RUN" = 1 ] || gone "$INSTALL_DIR/samurai"
fi

# 2. uv tool install
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^samurai\b'; then
  run uv tool uninstall samurai >/dev/null 2>&1 || true
  [ "$DRY_RUN" = 1 ] || gone "uv tool install"
fi

# 3. the PATH block, from whichever profile carries it
for profile in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
  [ -f "$profile" ] || continue
  grep -qF "$MARK_BEGIN" "$profile" || continue
  if [ "$DRY_RUN" = 1 ]; then
    echo "    + drop the PATH block from $profile"
    continue
  fi
  tmp=$(mktemp)
  awk -v begin="$MARK_BEGIN" -v end="$MARK_END" '
    index($0, begin) { skipping = 1; next }
    index($0, end) { skipping = 0; next }
    !skipping { print }
  ' "$profile" >"$tmp"
  cat "$tmp" >"$profile"
  rm -f "$tmp"
  gone "PATH block from $profile"
done

# 4. belt images (large, and useless without the product)
for image in $images; do
  run docker image rm "$image" >/dev/null 2>&1 || true
  [ "$DRY_RUN" = 1 ] || gone "docker image $image"
done

# 5. state
if [ -d "$STATE_DIR" ]; then
  if [ "$KEEP_STATE" = 1 ]; then
    kept "$STATE_DIR"
  else
    run rm -rf "$STATE_DIR"
    [ "$DRY_RUN" = 1 ] || gone "$STATE_DIR"
  fi
fi

echo
if [ "$DRY_RUN" = 1 ]; then
  info "dry run — nothing was changed"
else
  info "done. belt images come back on the next sandbox run; a rebuild is a plain reinstall."
fi
