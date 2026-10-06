#!/usr/bin/env bash
# Download all Daily EventLogFile CSVs for one log date.
#
# Usage: scripts/download.sh [YYYY-MM-DD] [target-org]
#   date        log date in UTC (default: yesterday, UTC)
#   target-org  org alias (required)
#
# Output: $EVENT_LOG_HOME/logs/<org>/<date>/<EventType>.csv plus manifest.json, users.json
#         and login_geo.json. EVENT_LOG_HOME defaults to ~/.local/share/sf-event-logs.
# Re-running for the same date overwrites that day's folder (review.md is kept).
set -euo pipefail

LOG_DATE="${1:-$(date -u -d yesterday +%F)}"
ORG="${2:?usage: download.sh YYYY-MM-DD target-org}"
API_VERSION="67.0"

DATA="${EVENT_LOG_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/sf-event-logs}"
OUT_DIR="$DATA/logs/$ORG/$LOG_DATE"
NEXT_DATE="$(date -u -d "$LOG_DATE + 1 day" +%F)"

QUERY="SELECT Id, EventType, Interval, Sequence, LogDate, CreatedDate, LogFileLength
FROM EventLogFile
WHERE Interval = 'Daily'
  AND LogDate >= ${LOG_DATE}T00:00:00Z AND LogDate < ${NEXT_DATE}T00:00:00Z
ORDER BY EventType, Sequence"

echo "Querying $ORG for Daily event logs dated $LOG_DATE..." >&2
RESULT="$(sf data query --target-org "$ORG" --json -q "$QUERY")"

if [[ "$(jq -r '.status' <<<"$RESULT")" != "0" ]]; then
  echo "Query failed: $(jq -r '.message // .' <<<"$RESULT")" >&2
  exit 1
fi

COUNT="$(jq '.result.totalSize' <<<"$RESULT")"
if [[ "$COUNT" == "0" ]]; then
  # Salesforce writes no file at all for an event type with no events, so a quiet
  # day (e.g. a weekend) looks the same as "not generated yet". Treat the day as
  # genuinely empty once a later day has files or it is well past generation time.
  LATER="$(sf data query --target-org "$ORG" --json \
    -q "SELECT COUNT() FROM EventLogFile WHERE Interval = 'Daily' AND LogDate >= ${NEXT_DATE}T00:00:00Z" |
    jq '.result.totalSize // 0')"
  if [[ "$LATER" == "0" && "$(date -u +%s)" -lt "$(date -u -d "$NEXT_DATE 12:00" +%s)" ]]; then
    echo "No Daily event logs for $LOG_DATE yet (files usually appear ~03:00 UTC the next day)." >&2
    exit 2
  fi
  echo "No Daily event logs for $LOG_DATE: no recorded activity that day." >&2
fi

# Download into a staging dir and only swap it in once every file succeeded,
# so a failed run never leaves a half-populated day behind.
mkdir -p "$DATA/logs/$ORG"
STAGE="$(mktemp -d "$DATA/logs/$ORG/.staging-$LOG_DATE.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

fetch() { # fetch <EventLogFile Id> <dest file>; retries transient network errors
  local attempt
  for attempt in 1 2 3 4; do
    if sf api request rest --target-org "$ORG" \
      "/services/data/v$API_VERSION/sobjects/EventLogFile/$1/LogFile" >"$2" 2>"$2.err"; then
      rm -f "$2.err"
      return 0
    fi
    echo "  retry $attempt for $1: $(tail -1 "$2.err")" >&2
    sleep $((attempt * 5))
  done
  echo "Failed to download $1 after 4 attempts" >&2
  return 1
}

while read -r rec; do
  id="$(jq -r '.Id' <<<"$rec")"
  type="$(jq -r '.EventType' <<<"$rec")"
  seq="$(jq -r '.Sequence // 0' <<<"$rec")"
  file="$STAGE/$type.csv"
  [[ "$seq" != "0" ]] && file="$STAGE/${type}_$seq.csv"
  fetch "$id" "$file"
  rows="$(python3 -c 'import csv,sys; csv.field_size_limit(sys.maxsize); print(sum(1 for _ in csv.DictReader(open(sys.argv[1], newline=""))))' "$file")"
  echo "  $type -> $(basename "$file") ($rows rows)" >&2
done < <(jq -c '.result.records[]' <<<"$RESULT")

# Username lookup so the digest can show people rather than raw User Ids.
sf data query --target-org "$ORG" --json \
  -q "SELECT Id, Username, Name, IsActive, UserType, Profile.Name FROM User" |
  jq '[.result.records[] | {Id, Username, Name, IsActive, UserType, Profile: .Profile.Name}]' >"$STAGE/users.json"

# Login countries: the Login event log has IPs but no geography, so take it from
# LoginHistory/LoginGeo. Optional - the digest notes the gap if this fails.
if GEO="$(sf data query --target-org "$ORG" --json -q "SELECT UserId, LoginTime, SourceIp, Status, LoginType,
    LoginGeo.CountryIso, LoginGeo.City FROM LoginHistory
    WHERE LoginTime >= ${LOG_DATE}T00:00:00Z AND LoginTime < ${NEXT_DATE}T00:00:00Z" 2>/dev/null)" &&
  [[ "$(jq -r '.status' <<<"$GEO")" == "0" ]]; then
  jq '[.result.records[] | {UserId, LoginTime, SourceIp, Status, LoginType,
    Country: .LoginGeo.CountryIso, City: .LoginGeo.City}]' <<<"$GEO" >"$STAGE/login_geo.json"
else
  echo "LoginHistory query failed; login countries unavailable for $LOG_DATE" >&2
fi

ORG_INFO="$(sf data query --target-org "$ORG" --json \
  -q "SELECT Id, Name, IsSandbox, OrganizationType, InstanceName FROM Organization" |
  jq '.result.records[0] // {} | del(.attributes)')"

jq --arg org "$ORG" --arg date "$LOG_DATE" --argjson orgInfo "$ORG_INFO" \
  '{org: $org, logDate: $date, downloadedAt: (now | todate), orgInfo: $orgInfo,
    files: [.result.records[] | {Id, EventType, Sequence, LogFileLength, CreatedDate}]}' \
  <<<"$RESULT" >"$STAGE/manifest.json"

# Keep any review already written for this day when re-downloading it.
if [[ -f "$OUT_DIR/review.md" ]]; then cp "$OUT_DIR/review.md" "$STAGE/"; fi
rm -rf "$OUT_DIR"
mv "$STAGE" "$OUT_DIR"
trap - EXIT

echo "Downloaded $COUNT file(s) to $OUT_DIR" >&2
echo "$OUT_DIR"
