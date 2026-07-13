#!/bin/bash
#
# ntfy-digest.sh - posts a Ka-Ching! summary to ntfy
#
# Usage:
#   ./ntfy-digest.sh weekly
#   ./ntfy-digest.sh monthly
#
# Config via environment variables (set these before calling, or export
# them in the crontab / a wrapper script):
#   KACHING_URL   - where Ka-Ching! is reachable (default: http://192.168.0.178:8091)
#   NTFY_URL      - your ntfy server (default: https://ntfy.sh)
#   NTFY_TOPIC    - required, your ntfy topic name
#
# Requires: curl, jq
#
# Example crontab (adjust paths/times to suit - the folder path below is
# whatever this actually lives in on your server):
#   0 8 * * 1   NTFY_TOPIC=kaching /opt/stacks/kaching/scripts/ntfy-digest.sh weekly
#   0 8 1 * *   NTFY_TOPIC=kaching /opt/stacks/kaching/scripts/ntfy-digest.sh monthly

set -euo pipefail

MODE="${1:-weekly}"
KACHING_URL="${KACHING_URL:-http://192.168.0.178:8091}"
NTFY_URL="${NTFY_URL:-https://ntfy.sh}"
NTFY_TOPIC="${NTFY_TOPIC:?Set NTFY_TOPIC to your ntfy topic name}"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required (apt install jq)" >&2
  exit 1
fi

SUMMARY=$(curl -sf "${KACHING_URL}/api/summary") || {
  echo "Could not reach Ka-Ching! at ${KACHING_URL}" >&2
  exit 1
}

case "$MODE" in
  weekly)
    TOTAL=$(echo "$SUMMARY" | jq -r '.week_total')
    COUNT=$(echo "$SUMMARY" | jq -r '.week_item_count')
    if [ "$COUNT" = "0" ]; then
      MESSAGE="Nothing due this week."
    else
      MESSAGE="This week: £${TOTAL} across ${COUNT} issue(s)."
    fi
    TITLE="Ka-Ching! - this week"
    ;;
  monthly)
    TOTAL=$(echo "$SUMMARY" | jq -r '.month_total_estimate')
    COUNT=$(echo "$SUMMARY" | jq -r '.month_item_count')
    MONTH=$(echo "$SUMMARY" | jq -r '.month')
    MESSAGE="${MONTH} forecast: £${TOTAL} across ${COUNT} issue(s), incl. est. shipping."
    TITLE="Ka-Ching! - month ahead"
    ;;
  *)
    echo "Unknown mode '${MODE}' - use 'weekly' or 'monthly'" >&2
    exit 1
    ;;
esac

curl -s \
  -H "Title: ${TITLE}" \
  -H "Tags: books" \
  -d "$MESSAGE" \
  "${NTFY_URL}/${NTFY_TOPIC}" > /dev/null

echo "Sent: $MESSAGE"
