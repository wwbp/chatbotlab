#!/usr/bin/env bash
# Post-deploy smoke check: is the deployed site actually working?
#
#   scripts/smoke.sh https://dev.bot.wwbp.org
#
# Polls the API readiness endpoint (database + cache reachable) and then the
# frontend root, retrying until SMOKE_TIMEOUT seconds have passed in total —
# a new version takes a little while to come up. Exits non-zero on failure.
# Needs only bash and curl.
set -u

base="${1:?usage: smoke.sh <base-url>   e.g. smoke.sh https://dev.bot.wwbp.org}"
base="${base%/}"
timeout="${SMOKE_TIMEOUT:-300}"
interval="${SMOKE_INTERVAL:-10}"
deadline=$((SECONDS + timeout))
body="$(mktemp)"
trap 'rm -f "$body"' EXIT

# wait_for <url> <pattern>: pass once <url> returns 200 with a body matching
# <pattern>. The status alone is not enough: a path that is not routed to the
# backend can come back as 200 with the frontend's HTML.
wait_for() {
  local url="$1" pattern="$2" code
  while :; do
    # curl reports 000 when it cannot connect or times out.
    code="$(curl -s -o "$body" --max-time 10 -w '%{http_code}' "$url")"
    if [ "$code" = "200" ] && grep -q "$pattern" "$body"; then
      echo "ok    $url"
      return 0
    fi
    echo "wait  $url -> HTTP $code"
    if [ $((SECONDS + interval)) -gt "$deadline" ]; then
      echo "FAIL  $url not healthy after ${timeout}s (last: HTTP $code)"
      head -c 300 "$body"
      echo
      return 1
    fi
    sleep "$interval"
  done
}

wait_for "$base/api/health/ready/" '"status": *"ok"' || exit 1
wait_for "$base/" 'id="root"' || exit 1
echo "Smoke check passed for $base"
