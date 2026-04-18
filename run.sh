#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ── Check setup was completed ─────────────────────────────────────────────────
if [ ! -f "$DIR/settings.json" ] || [ ! -d "$DIR/venv" ]; then
  echo "Setup not complete. Please run ./setup.sh first."
  echo "Setup não concluído. Por favor, rode ./setup.sh primeiro."
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

# ── Meeting language selection ─────────────────────────────────────────────────
echo ""
echo "$(t run.meeting_lang_title)"
t run.meeting_lang_options
echo ""
echo -n "$(t run.meeting_lang_prompt)"
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

echo ""

# ── Meeting name ──────────────────────────────────────────────────────────────
echo -n "$(t run.meeting_name_prompt)"
read -r meeting_name

# ── Run app ────────────────────────────────────────────────────────────────────
source venv/bin/activate
python -u app.py --ui-lang "$UI_LANG" --meeting-lang "$MEETING_LANG" --meeting-name "$meeting_name"
