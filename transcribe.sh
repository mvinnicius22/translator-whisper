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

if [ ! -f "$DIR/settings.json" ] || [ ! -d "$DIR/venv" ]; then
  echo "${RED}Setup not complete. Please run ./setup.sh first.${NC}"
  exit 1
fi

# ── File path ──────────────────────────────────────────────────────────────────
FILE_PATH="${*:-}"

if [ -z "$FILE_PATH" ]; then
  echo ""
  echo -n "${BOLD}Path to video/audio file: ${NC}"
  read -r FILE_PATH
fi

if [ ! -f "$FILE_PATH" ]; then
  echo "${RED}Error: file not found: $FILE_PATH${NC}"
  exit 1
fi

# ── Model selection ────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}${CYAN}Whisper model:${NC}"
echo "  ${YELLOW}1)${NC} medium   good accuracy  | M3 Pro: ~10-18 min / 90 min video  ${BOLD}(default)${NC}"
echo "  ${YELLOW}2)${NC} large    best accuracy  | M3 Pro: ~20-35 min / 90 min video"
echo ""
echo -n "${BOLD}Choice [1]: ${NC}"
read -r model_choice

case "$model_choice" in
  2) MODEL="large" ;;
  *) MODEL="medium" ;;
esac

# ── Language ───────────────────────────────────────────────────────────────────
echo ""
echo -n "${BOLD}Audio language code [pt]: ${NC}"
read -r lang_input
LANG="${lang_input:-pt}"

# ── Output format ─────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}${CYAN}Output format:${NC}"
echo "  ${YELLOW}1)${NC} timestamped  each segment prefixed with [MM:SS], good for navigation  ${BOLD}(default)${NC}"
echo "  ${YELLOW}2)${NC} prose        clean continuous text, no timestamps, good for reading"
echo ""
echo -n "${BOLD}Choice [1]: ${NC}"
read -r fmt_choice

case "$fmt_choice" in
  2) FORMAT="prose" ;;
  *) FORMAT="timestamped" ;;
esac

# ── Meeting/video name ─────────────────────────────────────────────────────────
echo ""
echo -n "${BOLD}Output folder name (press Enter to use filename): ${NC}"
read -r meeting_name

# ── Run ────────────────────────────────────────────────────────────────────────
echo ""
source venv/bin/activate
python -u transcribe_file.py \
  --file "$FILE_PATH" \
  --model "$MODEL" \
  --lang "$LANG" \
  --format "$FORMAT" \
  --meeting-name "$meeting_name"
