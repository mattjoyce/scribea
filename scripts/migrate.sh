#!/usr/bin/env bash
# Apply migrations in db/migrations/ in lexical order. Version-aware: each
# file is named NNN_<slug>.sql and applies its DDL then bumps PRAGMA
# user_version to NNN. A file is skipped if user_version >= NNN.
#
# This lets migrations contain non-idempotent DDL (e.g. ALTER TABLE ADD
# COLUMN) without per-statement IF NOT EXISTS guards — the runner
# guarantees each file is applied at most once. Files that *also* use
# IF NOT EXISTS guards (e.g. 001_initial.sql) remain safe.
#
# Usage:
#   ./scripts/migrate.sh                # uses ./scribe.db
#   DB_PATH=/tmp/test.db ./scripts/migrate.sh
set -euo pipefail

DB_PATH="${DB_PATH:-./scribe.db}"
MIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/db/migrations"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "error: sqlite3 not on PATH" >&2
  exit 1
fi

if [[ ! -d "$MIG_DIR" ]]; then
  echo "error: migrations directory not found: $MIG_DIR" >&2
  exit 1
fi

echo "applying migrations to: $DB_PATH"
shopt -s nullglob
files=("$MIG_DIR"/*.sql)
if [[ ${#files[@]} -eq 0 ]]; then
  echo "no migration files found in $MIG_DIR"
  exit 0
fi

current_ver=$(sqlite3 "$DB_PATH" "PRAGMA user_version;")

for f in "${files[@]}"; do
  fname=$(basename "$f")
  # Extract leading numeric prefix (e.g. 003_cases.sql -> 3).
  raw="${fname%%_*}"
  if ! [[ "$raw" =~ ^[0-9]+$ ]]; then
    echo "  skip $fname (no numeric prefix)"
    continue
  fi
  file_ver=$((10#$raw))
  if (( file_ver <= current_ver )); then
    echo "  skip $fname (current user_version=$current_ver)"
    continue
  fi
  echo "  - $fname"
  sqlite3 "$DB_PATH" < "$f"
  current_ver=$(sqlite3 "$DB_PATH" "PRAGMA user_version;")
done

echo "ok — schema version is now $current_ver"
