#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ── Check setup ────────────────────────────────────────────────────────────────
if [ ! -f "$DIR/settings.json" ] || [ ! -d "$DIR/venv" ]; then
  echo "Setup not complete. Please run ./setup.sh first."
  echo "Setup não concluído. Por favor, rode ./setup.sh primeiro."
  exit 1
fi

# ── Read UI language ──────────────────────────────────────────────────────────
UI_LANG=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
print(json.load(open(sys.argv[1]))["ui_language"])
PYEOF
)

# Helper
t() {
  python3 - "$DIR/locales/$UI_LANG.json" "$1" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
section, subkey = sys.argv[2].split(".", 1)
print(d[section][subkey])
PYEOF
}

MEETINGS_DIR="$HOME/Documents/Meetings"

# ── Select meeting ─────────────────────────────────────────────────────────────
echo ""
echo "$(t process.recent_meetings)"
echo ""

# List all meeting folders (handles both flat and daily structures)
mapfile -t MEETING_FOLDERS < <(find "$MEETINGS_DIR" -name "transcript.md" -not -path "*/\.*" | sort -r | head -20 | xargs -I{} dirname {})

if [ ${#MEETING_FOLDERS[@]} -eq 0 ]; then
  echo "$(t process.meeting_not_found)"
  exit 1
fi

for i in "${!MEETING_FOLDERS[@]}"; do
  echo "  $((i+1))) ${MEETING_FOLDERS[$i]##*/}"
done

echo ""
echo -n "$(t process.select_meeting)"
read -r meeting_choice

if [ -z "$meeting_choice" ]; then
  MEETING_FOLDER="${MEETING_FOLDERS[0]}"
else
  MEETING_FOLDER="${MEETING_FOLDERS[$((meeting_choice-1))]}"
fi

TRANSCRIPT="$MEETING_FOLDER/transcript.md"

if [ ! -f "$TRANSCRIPT" ]; then
  echo "$(t process.no_transcript)"
  exit 1
fi

# ── Select agent ───────────────────────────────────────────────────────────────
echo ""
echo "$(t process.available_agents)"
echo ""

mapfile -t AGENT_FILES < <(ls "$DIR/agents/"*.md 2>/dev/null | sort)

if [ ${#AGENT_FILES[@]} -eq 0 ]; then
  echo "$(t process.no_agents)"
  exit 1
fi

for i in "${!AGENT_FILES[@]}"; do
  AGENT_BASE=$(basename "${AGENT_FILES[$i]}" .md)
  echo "  $((i+1))) $AGENT_BASE"
done

echo ""
echo -n "$(t process.select_agent)"
read -r agent_choice

if [ -z "$agent_choice" ]; then
  agent_choice=1
fi

AGENT_FILE="${AGENT_FILES[$((agent_choice-1))]}"
AGENT_NAME=$(basename "$AGENT_FILE" .md)
OUTPUT_FILE="$MEETING_FOLDER/$AGENT_NAME.md"

echo ""
RUNNING_MSG=$(t process.running_agent)
echo "${RUNNING_MSG//\{agent\}/$AGENT_NAME}"
echo ""

# ── Run agent ──────────────────────────────────────────────────────────────────
source venv/bin/activate
python -u process.py "$TRANSCRIPT" "$AGENT_FILE" "$OUTPUT_FILE"

echo ""
SAVED_MSG=$(t process.saved)
echo "${SAVED_MSG//\{path\}/$OUTPUT_FILE}"
