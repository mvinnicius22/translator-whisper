#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
CYAN=$'\033[0;36m'
RED=$'\033[0;31m'
NC=$'\033[0m'

# ── Check setup was completed ─────────────────────────────────────────────────
if [ ! -f "$DIR/settings.json" ] || [ ! -d "$DIR/venv" ]; then
  echo "${RED}Setup not complete. Please run ./setup.sh first.${NC}"
  exit 1
fi

# ── Read UI language from settings ────────────────────────────────────────────
UI_LANG=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
print(json.load(open(sys.argv[1]))["ui_language"])
PYEOF
)

# Helper: read a key from the locale JSON file
t() {
  python3 - "$DIR/locales/$UI_LANG.json" "$1" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
section, subkey = sys.argv[2].split(".", 1)
print(d[section][subkey])
PYEOF
}

# ── Check meeting mode is installed ──────────────────────────────────────────
MEETING_INSTALLED=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print("yes" if "meeting" in d.get("installed_modes", []) else "no")
PYEOF
)

if [ "$MEETING_INSTALLED" != "yes" ]; then
  echo "${RED}Real-time meeting mode is not installed.${NC}"
  echo "Run ${BOLD}./setup.sh${NC} and choose 'Add real-time meeting support'."
  exit 1
fi

DIARIZE_INSTALLED=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print("yes" if "diarize" in d.get("installed_modes", []) else "no")
PYEOF
)

# ── Meeting language selection ─────────────────────────────────────────────────
echo ""
echo "${BOLD}${CYAN}$(t run.meeting_lang_title)${NC}"
t run.meeting_lang_options
echo ""
echo -n "${BOLD}$(t run.meeting_lang_prompt)${NC}"
read -r lang_choice

case "$lang_choice" in
  2) MEETING_LANG="pt" ;;
  3) MEETING_LANG="es" ;;
  4) MEETING_LANG="fr" ;;
  5) MEETING_LANG="de" ;;
  6) MEETING_LANG="it" ;;
  7) MEETING_LANG="ja" ;;
  8) MEETING_LANG="zh" ;;
  9) MEETING_LANG="nl" ;;
  *) MEETING_LANG="en" ;;
esac

# ── Speaker detection mode ────────────────────────────────────────────────────
SPEAKER_MODE="live"
POST_MODEL="large-v3"
POST_FORMAT="prose"

if [ "$DIARIZE_INSTALLED" = "yes" ]; then
  echo ""
  echo "${BOLD}${CYAN}Speaker detection / Detecção de falantes:${NC}"
  echo "  ${YELLOW}1)${NC} Standard  live labels, current behavior  ${BOLD}(default)${NC}"
  echo "  ${YELLOW}2)${NC} High accuracy  records meeting and diarizes after it ends"
  echo ""
  echo -n "${BOLD}Choice [1]: ${NC}"
  read -r speaker_choice

  if [ "$speaker_choice" = "2" ]; then
    SPEAKER_MODE="post"
    echo ""
    echo "${BOLD}${CYAN}Whisper model for transcription:${NC}"
    echo "  ${YELLOW}1)${NC} large-v3   best accuracy  ${BOLD}(default)${NC}"
    echo "  ${YELLOW}2)${NC} medium     good accuracy (faster)"
    echo ""
    echo -n "${BOLD}Choice [1]: ${NC}"
    read -r post_model_choice
    case "$post_model_choice" in
      2) POST_MODEL="medium" ;;
      *) POST_MODEL="large-v3" ;;
    esac
    echo ""
    echo "${BOLD}${CYAN}Output format:${NC}"
    echo "  ${YELLOW}1)${NC} prose        clean continuous text by speaker  ${BOLD}(default)${NC}"
    echo "  ${YELLOW}2)${NC} timestamped  each segment prefixed with [MM:SS]"
    echo ""
    echo -n "${BOLD}Choice [1]: ${NC}"
    read -r post_fmt_choice
    case "$post_fmt_choice" in
      2) POST_FORMAT="timestamped" ;;
      *) POST_FORMAT="prose" ;;
    esac
    echo ""
    echo "${CYAN}High accuracy mode: meeting audio will be recorded and diarized after you stop.${NC}"
    echo "${CYAN}Speaker labels appear in the transcript once processing is complete.${NC}"
  fi
fi

echo ""

# ── Meeting name ──────────────────────────────────────────────────────────────
echo -n "${BOLD}$(t run.meeting_name_prompt)${NC}"
read -r meeting_name

# ── Run app ────────────────────────────────────────────────────────────────────
source venv/bin/activate
python -u app.py \
  --ui-lang "$UI_LANG" \
  --meeting-lang "$MEETING_LANG" \
  --meeting-name "$meeting_name" \
  --speaker-mode "$SPEAKER_MODE" \
  --model "$POST_MODEL" \
  --format "$POST_FORMAT"
