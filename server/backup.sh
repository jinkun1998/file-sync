#!/usr/bin/env bash
set -euo pipefail

: "${STORAGE_ROOT:=/mnt/hdd/fw/thaodlq2/storage}"
: "${PUSH_RETRIES:=2}"

lock_dir="$STORAGE_ROOT/.file-sync-backup.lock"
registry="$STORAGE_ROOT/.file-sync-state/repositories"
log() { printf '%s file-sync-backup: %s\n' "$(date '+%F %T')" "$*"; }
cleanup() { rmdir "$lock_dir" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

[ -d "$STORAGE_ROOT" ] || { log "storage missing: $STORAGE_ROOT"; exit 1; }
if ! mkdir "$lock_dir" 2>/dev/null; then
  log "another backup is running; exiting"
  exit 0
fi
[ -f "$registry" ] || { log "no registered repositories"; exit 0; }

backup_repository() {
  local repo=$1 branch attempt
  case $repo in "$STORAGE_ROOT"/*) ;; *) log "unsafe registered repository: $repo"; return 1 ;; esac
  [ -d "$repo/.git" ] || { log "Git repository missing: $repo"; return 1; }
  git -C "$repo" rev-parse --is-inside-work-tree >/dev/null || return 1
  branch=$(git -C "$repo" symbolic-ref --quiet --short HEAD) || { log "detached branch: $repo"; return 1; }
  git -C "$repo" remote get-url origin >/dev/null || { log "origin missing: $repo"; return 1; }
  git -C "$repo" add -A
  if git -C "$repo" diff --cached --quiet; then
    log "unchanged: $repo"
    return 0
  fi
  git -C "$repo" commit -m "backup: $(date '+%F %T')"
  for ((attempt = 0; attempt <= PUSH_RETRIES; attempt++)); do
    if git -C "$repo" push origin "HEAD:$branch"; then
      log "backup committed and pushed: $repo"
      return 0
    fi
    if (( attempt == PUSH_RETRIES )); then
      log "push failed; committed data remains local: $repo"
      return 1
    fi
    sleep "$((2 ** attempt))"
  done
}

status=0
while IFS= read -r repo || [ -n "$repo" ]; do
  [ -n "$repo" ] || continue
  backup_repository "$repo" || status=1
done < "$registry"
exit "$status"
