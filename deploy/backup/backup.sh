#!/usr/bin/env bash
set -Eeuo pipefail

backup_dir="$(mktemp -d)"
trap 'rm -rf -- "$backup_dir"' EXIT
dump_path="$backup_dir/kosto-vet.dump"
export RESTIC_CACHE_DIR="$backup_dir/restic-cache"
mkdir -p "$RESTIC_CACHE_DIR"

if ! restic snapshots >/dev/null 2>&1; then
  restic init
fi

pg_dump --format=custom --no-owner --no-acl --file="$dump_path"
restic backup "$dump_path" --tag postgres --tag kosto-vet
restic forget --tag kosto-vet --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
restic check
