#!/bin/sh
set -u

project="${WATCHDOG_PROJECT:?set WATCHDOG_PROJECT}"
label="${WATCHDOG_LABEL:?set WATCHDOG_LABEL}"
interval="${WATCHDOG_INTERVAL_SECONDS:-10}"
grace="${WATCHDOG_RESTART_GRACE_SECONDS:-30}"
cooldown="${WATCHDOG_RESTART_COOLDOWN_SECONDS:-60}"

case "$interval:$grace:$cooldown" in
  *[!0-9:]*|*::*|:*|*:) echo "Invalid watchdog numeric configuration." >&2; exit 2 ;;
esac
if [ "$interval" -lt 5 ] || [ "$grace" -lt 5 ] || [ "$cooldown" -lt 30 ]; then
  echo "Watchdog settings are below their safe minimums." >&2
  exit 2
fi

echo "Watchdog started for project=$project label=$label."
while true; do
  touch /tmp/watchdog-heartbeat
  if ! ids="$(timeout 10 docker ps --quiet \
      --filter "label=com.docker.compose.project=$project" \
      --filter "label=$label" 2>/dev/null)"; then
    echo "Watchdog cannot query Docker; retrying." >&2
    sleep "$interval"
    continue
  fi

  for id in $ids; do
    health="$(timeout 10 docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null)"
    [ "$health" = "unhealthy" ] || continue

    name="$(timeout 10 docker inspect --format '{{.Name}}' "$id" 2>/dev/null)"
    restart_file="/tmp/restarted-$id"
    now="$(date +%s)"
    last=0
    [ -f "$restart_file" ] && last="$(cat "$restart_file" 2>/dev/null || echo 0)"
    if [ "$((now - last))" -lt "$cooldown" ]; then
      echo "Restart cooldown active name=$name." >&2
      continue
    fi
    echo "Restarting unhealthy container name=$name." >&2
    if timeout "$((grace + 15))" docker restart --time "$grace" "$id" >/dev/null; then
      printf '%s' "$now" > "$restart_file"
    else
      echo "Restart failed name=$name." >&2
    fi
  done

  sleep "$interval"
done
