#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ ! -f "$DIR/settings.json" ] || [ ! -d "$DIR/venv" ]; then
  echo "Setup not complete. Please run ./setup.sh first."
  exit 1
fi

# ── File path ──────────────────────────────────────────────────────────────────
FILE_PATH="${1:-}"

if [ -z "$FILE_PATH" ]; then
  echo ""
  echo -n "Path to video/audio file: "
  read -r FILE_PATH
fi

if [ ! -f "$FILE_PATH" ]; then
  echo "Error: file not found: $FILE_PATH"
  exit 1
fi

# ── Model selection ────────────────────────────────────────────────────────────
echo ""
echo "Select Whisper model:"
echo "  1) medium  :very good accuracy  | M3 Pro: ~10–18 min for 90 min video"
echo "  2) large   :best accuracy (v3)  | M3 Pro: ~20–35 min for 90 min video"
echo ""
echo -n "Choice [1]: "
read -r model_choice

case "$model_choice" in
  2) MODEL="large" ;;
  *) MODEL="medium" ;;
esac

# ── Language ───────────────────────────────────────────────────────────────────
echo ""
echo -n "Audio language code [pt]: "
read -r lang_input
LANG="${lang_input:-pt}"

# ── Output format ─────────────────────────────────────────────────────────────
echo ""
echo "Output format:"
echo "  1) timestamped :each segment prefixed with [MM:SS]:good for navigation  (default)"
echo "  2) prose       :clean continuous text, no timestamps:good for reading/editing"
echo ""
echo -n "Choice [1]: "
read -r fmt_choice

case "$fmt_choice" in
  2) FORMAT="prose" ;;
  *) FORMAT="timestamped" ;;
esac

# ── Meeting/video name ─────────────────────────────────────────────────────────
echo ""
echo -n "Output folder name (press Enter to use filename): "
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
