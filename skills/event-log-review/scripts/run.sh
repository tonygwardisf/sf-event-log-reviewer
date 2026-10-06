#!/usr/bin/env bash
# One-shot daily pipeline: download the day's event logs, backfill any missing
# baseline days, prune old downloads, and write logs/<org>/<date>/digest.md.
# Data lives outside the skill in $EVENT_LOG_HOME (default ~/.local/share/sf-event-logs):
#   logs/<org>/<date>/   downloaded CSVs, digest.md, review.md
#   known/<org>.md       confirmed-normal patterns (created from known/_template.md)
#
# Usage: scripts/run.sh [YYYY-MM-DD] [target-org]   (either order, both optional)
#   date defaults to yesterday (UTC); org to $EVENT_LOG_ORG, else the sf CLI's target-org.
# Prints:     digest=<path> and known=<path> on stdout.
# Exit codes: 0 ok, 2 logs for that date not generated yet, other = failure.
set -euo pipefail

LOG_DATE="" ORG=""
for arg in "$@"; do
  if [[ "$arg" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then LOG_DATE="$arg"; else ORG="$arg"; fi
done
LOG_DATE="${LOG_DATE:-$(date -u -d yesterday +%F)}"
ORG="${ORG:-${EVENT_LOG_ORG:-$(sf config get target-org --json 2>/dev/null | jq -r '.result[0].value // empty')}}"
if [[ -z "$ORG" ]]; then
  echo "No org given: pass an alias, set EVENT_LOG_ORG, or run: sf config set target-org <alias> --global" >&2
  exit 1
fi
BASELINE_DAYS=7
KEEP_DAYS=30   # Salesforce keeps EventLogFiles for 30 days; keep the same locally

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${EVENT_LOG_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/sf-event-logs}"
export EVENT_LOG_HOME="$DATA"
LOGS="$DATA/logs/$ORG"
KNOWN="$DATA/known/$ORG.md"

"$DIR/download.sh" "$LOG_DATE" "$ORG" >/dev/null

for i in $(seq 1 "$BASELINE_DAYS"); do
  d="$(date -u -d "$LOG_DATE - $i day" +%F)"
  if [[ ! -d "$LOGS/$d" ]]; then
    "$DIR/download.sh" "$d" "$ORG" >/dev/null || echo "Baseline day $d unavailable (continuing)" >&2
  fi
done

cutoff="$(date -u -d "$LOG_DATE - $KEEP_DAYS day" +%F)"
for d in "$LOGS"/20??-??-??; do
  if [[ -d "$d" && "$(basename "$d")" < "$cutoff" ]]; then rm -rf "$d"; fi
done

python3 "$DIR/digest.py" "$LOG_DATE" --org "$ORG" --baseline-days "$BASELINE_DAYS" >"$LOGS/$LOG_DATE/digest.md"

if [[ ! -f "$KNOWN" ]]; then
  mkdir -p "$(dirname "$KNOWN")"
  sed "s/<org alias>/$ORG/g" "$DIR/../known/_template.md" >"$KNOWN"
fi

echo "digest=$LOGS/$LOG_DATE/digest.md"
echo "known=$KNOWN"
