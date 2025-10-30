#!/usr/bin/env bash
set -euo pipefail

# CookieCloud replace installer
# This script downloads modified files from the replacement repository and applies them with backup.
# Default base: https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/OnlineMo/Goofish-Auto-reply-replace/refs/heads/main/replace.sh | bash
#   REPLACE_BASE_URL=https://raw.githubusercontent.com/.../branch ./replace.sh

BASE="${REPLACE_BASE_URL:-https://raw.githubusercontent.com/hermer/Goofish-Auto-reply-replace/refs/heads/main}"
FILELIST_URL="$BASE/replace/filelist.txt"

timestamp() { date +%Y%m%d_%H%M%S; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

download() {
  # $1 url, $2 out_path
  local url="$1"
  local out="$2"
  if has_cmd curl; then
    curl -fsSL "$url" -o "$out"
  elif has_cmd wget; then
    wget -qO "$out" "$url"
  else
    echo "Neither curl nor wget found." >&2
    return 1
  fi
}

fetch_text() {
  # $1 url, prints content
  local url="$1"
  if has_cmd curl; then
    curl -fsSL "$url"
  elif has_cmd wget; then
    wget -qO- "$url"
  else
    echo "Neither curl nor wget found." >&2
    return 1
  fi
}

echo "==> Base: $BASE"
echo "==> Fetching file list: $FILELIST_URL"

FILELIST_CONTENT="$(fetch_text "$FILELIST_URL" | sed 's/\r$//')"
if [ -z "${FILELIST_CONTENT:-}" ]; then
  echo "Failed to fetch file list." >&2
  exit 1
fi

# Build array of paths
IFS=$'\n' read -r -d '' -a FILES < <(printf '%s\n' "$FILELIST_CONTENT" | sed '/^\s*#/d;/^\s*$/d' && printf '\0')

BACKUP_ROOT="backup_replace_$(timestamp)"
mkdir -p "$BACKUP_ROOT"
echo "Backup folder: $BACKUP_ROOT"

REPLACED=0
FAILED=0

for REL in "${FILES[@]}"; do
  REL="${REL//$'\r'/}"
  [ -z "$REL" ] && continue

  REMOTE="$BASE/replace/$REL"
  DEST="$REL"

  echo
  echo "[Download] $REMOTE"
  echo "[Target  ] $DEST"

  mkdir -p "$(dirname "$DEST")" || true
  mkdir -p "$BACKUP_ROOT/$(dirname "$DEST")" || true

  if [ -f "$DEST" ]; then
    cp -a "$DEST" "$BACKUP_ROOT/$DEST" || true
    echo "[Backup ] $DEST -> $BACKUP_ROOT/$DEST"
  fi

  TMP="$DEST.__tmp__"
  if download "$REMOTE" "$TMP"; then
    mv -f "$TMP" "$DEST"
    echo "[Done   ] wrote $DEST"
    REPLACED=$((REPLACED+1))
  else
    echo "[Error  ] failed to download $REMOTE" >&2
    rm -f "$TMP" || true
    FAILED=$((FAILED+1))
  fi
done

echo
echo "========== Summary =========="
echo "Replaced: $REPLACED"
echo "Failed  : $FAILED"
echo "Backup  : $BACKUP_ROOT"
echo "Done."

if [ "$FAILED" -gt 0 ]; then
  exit 2
fi
