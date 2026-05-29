#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
CYAN=$'\033[0;36m'
RED=$'\033[0;31m'
NC=$'\033[0m'

MEETINGS_DIR="$HOME/Documents/Meetings"
WEB_PORT=5050

if [ ! -f "$DIR/settings.json" ] || [ ! -d "$DIR/venv" ]; then
  echo "${RED}Setup not complete. Please run ./setup.sh first.${NC}"
  exit 1
fi

echo ""
echo "${BOLD}${CYAN}Slack Demo — Standup Recorder${NC}"
echo "${CYAN}Language: Portuguese · High accuracy · large-v3 · Prose${NC}"
echo ""
echo -n "${BOLD}Meeting name (optional): ${NC}"
read -r meeting_name

MARKER=$(/usr/bin/mktemp)
/usr/bin/touch "$MARKER"

source venv/bin/activate
python -u app.py \
  --ui-lang pt \
  --meeting-lang pt \
  --meeting-name "$meeting_name" \
  --speaker-mode post \
  --model large-v3 \
  --format prose

TRANSCRIPT=$(/usr/bin/find "$MEETINGS_DIR" -name "transcript.md" -newer "$MARKER" 2>/dev/null | /usr/bin/sort | /usr/bin/tail -1 || true)
/bin/rm -f "$MARKER"

if [ -z "$TRANSCRIPT" ]; then
  echo "${RED}No transcript found — nothing was recorded.${NC}"
  exit 1
fi

MEETING_FOLDER=$(/usr/bin/dirname "$TRANSCRIPT")
OUTPUT_FILE="$MEETING_FOLDER/client_update.md"
MEETING_LABEL="${meeting_name:-$(/usr/bin/basename "$MEETING_FOLDER")}"
TODAY=$(/bin/date +"%Y-%m-%d")

echo ""
echo "${CYAN}Generating client update...${NC}"
python process.py "$TRANSCRIPT" "$DIR/agents/client_update.md" "$OUTPUT_FILE"

# Start web server if not already running
if ! "$DIR/venv/bin/python" -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://localhost:$WEB_PORT/health', timeout=2)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
  echo ""
  echo "${CYAN}Starting web platform...${NC}"
  "$DIR/venv/bin/python" "$DIR/web/server.py" > /tmp/slack_web.log 2>&1 &
  sleep 2
fi

UPDATE_URL=$(python3 - "$OUTPUT_FILE" "$MEETING_LABEL" "$TODAY" "$TRANSCRIPT" "$WEB_PORT" <<'PYEOF'
import json, sys, urllib.request
content = open(sys.argv[1], encoding="utf-8").read()
port = sys.argv[5]
payload = json.dumps({
    "meeting_name": sys.argv[2],
    "date": sys.argv[3],
    "content": content,
    "transcript_path": sys.argv[4],
}).encode()
req = urllib.request.Request(
    f"http://localhost:{port}/api/submit",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
resp = json.loads(urllib.request.urlopen(req).read())
print(resp["url"])
PYEOF
)

echo ""
echo "${GREEN}Update ready for review:${NC}"
echo "  $UPDATE_URL"
echo ""
/usr/bin/open "$UPDATE_URL"
