#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ── Language selection (bilingual — shown before locale is loaded) ─────────────
echo ""
echo "Choose interface language / Escolha o idioma da interface:"
echo "  1) English (default)"
echo "  2) Português"
echo -n "Choice / Escolha [1]: "
read -r lang_choice

case "$lang_choice" in
  2) UI_LANG="pt" ;;
  *) UI_LANG="en" ;;
esac

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

echo ""
echo "$(t setup.welcome)"
echo ""

# ── Folder structure selection ────────────────────────────────────────────────
echo ""
echo "$(t setup.folder_structure_title)"
t setup.folder_structure_options
echo ""
echo -n "$(t setup.folder_structure_prompt)"
read -r folder_choice

case "$folder_choice" in
  2) FOLDER_STRUCTURE="daily" ;;
  *) FOLDER_STRUCTURE="flat" ;;
esac

# ── Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "→ Installing Homebrew / Instalando Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# ── System deps ───────────────────────────────────────────────────────────────
echo "$(t setup.installing_deps)"
brew install ffmpeg portaudio blackhole-2ch

echo "$(t setup.reloading_audio)"
sudo killall coreaudiod || true

# ── Python venv ───────────────────────────────────────────────────────────────
echo "$(t setup.creating_venv)"
python3 -m venv venv
source venv/bin/activate

echo "$(t setup.installing_packages)"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

# ── Save settings.json ────────────────────────────────────────────────────────
python3 - "$UI_LANG" "$DIR/settings.json" "$FOLDER_STRUCTURE" <<'PYEOF'
import json, sys
settings = {"ui_language": sys.argv[1], "folder_structure": sys.argv[3], "setup_complete": True}
with open(sys.argv[2], "w") as f:
    json.dump(settings, f, indent=2)
PYEOF

echo ""
echo "$(t setup.done)"
echo ""
echo "══════════════════════════════════════════════════════"
echo "  $(t setup.audio_title)"
echo "══════════════════════════════════════════════════════"
echo ""
t setup.audio_step1
echo ""
t setup.audio_step2
echo ""
t setup.audio_step3
echo ""
t setup.audio_step4
echo ""
t setup.audio_step5
echo ""
t setup.audio_step6
echo ""
echo "══════════════════════════════════════════════════════"
echo "  $(t setup.how_to_use_title)"
echo "══════════════════════════════════════════════════════"
echo ""
t setup.how_to_use
echo ""
