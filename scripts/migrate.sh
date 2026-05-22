#!/usr/bin/env bash
# Apply every migration in db/migrations/ in lexical order. Idempotent —
# migrations themselves use IF NOT EXISTS / UPSERT patterns.
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

for f in "${files[@]}"; do
  echo "  - $(basename "$f")"
  sqlite3 "$DB_PATH" < "$f"
done

ver=$(sqlite3 "$DB_PATH" "PRAGMA user_version;")
echo "ok — schema version is now $ver"
