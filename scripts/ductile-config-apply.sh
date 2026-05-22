#!/usr/bin/env bash
# Verify the scribe plugins are registered with the local Ductile gateway.
# This script does NOT mutate ~/.config/ductile/* — for that, follow the
# manual snippets in docs/ductile-config.md. This is just an idempotent
# probe to confirm wiring is correct after the operator's manual edit.
set -euo pipefail

DUCTILE_URL="${DUCTILE_URL:-http://127.0.0.1:8082}"
TOKEN="${DUCTILE_LOCAL_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  echo "warning: DUCTILE_LOCAL_TOKEN not set; will try unauthenticated probes" >&2
fi

auth=()
[[ -n "$TOKEN" ]] && auth=(-H "Authorization: Bearer $TOKEN")

probe() {
  local plugin="$1"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${auth[@]}" "$DUCTILE_URL/plugin/$plugin/health")
  printf "  %-22s -> HTTP %s\n" "$plugin" "$code"
  if [[ "$code" != "200" ]]; then
    return 1
  fi
}

echo "Probing scribe plugins on $DUCTILE_URL ..."
failed=0
for p in scribe-event-relay scribe-transcribe scribe-assemble scribe-structure scribe-format; do
  if ! probe "$p"; then failed=1; fi
done

if (( failed )); then
  cat <<EOF >&2

One or more plugins are not reachable. Likely causes:
  1. Plugin not in plugins.yaml or disabled — see docs/ductile-config.md
  2. plugin_roots in config.yaml missing /Volumes/Projects/scribea/plugins
  3. Ductile gateway not running — start with: ductile system status
  4. Gateway didn't reload — try: ductile system reload (or full restart)
EOF
  exit 1
fi

echo "ok — all scribe plugins reachable."
