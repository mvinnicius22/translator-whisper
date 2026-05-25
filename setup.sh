#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Helper: read a key from a locale JSON file (requires UI_LANG to be set)
t() {
  python3 - "$DIR/locales/$UI_LANG.json" "$1" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
section, subkey = sys.argv[2].split(".", 1)
print(d[section][subkey])
PYEOF
}

# Helper: print the audio configuration steps
print_audio_steps() {
  echo "══════════════════════════════════════════════════════"
  echo "  $(t setup.audio_title)"
  echo "══════════════════════════════════════════════════════"
  echo ""
  t setup.audio_step1; echo ""
  t setup.audio_step2; echo ""
  t setup.audio_step3; echo ""
  t setup.audio_step4; echo ""
  t setup.audio_step5; echo ""
  t setup.audio_step6; echo ""
  echo "══════════════════════════════════════════════════════"
  echo "  $(t setup.how_to_use_title)"
  echo "══════════════════════════════════════════════════════"
  echo ""
  t setup.how_to_use; echo ""
}

# ── Detect existing installation ──────────────────────────────────────────────
if [ -d "$DIR/venv" ] && [ -f "$DIR/settings.json" ]; then

  # Read stored preferences
  UI_LANG=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("ui_language", "en"))
PYEOF
  )
  FOLDER_STRUCTURE=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("folder_structure", "flat"))
PYEOF
  )
  MEETING_INSTALLED=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print("yes" if "meeting" in d.get("installed_modes", []) else "no")
PYEOF
  )

  echo ""
  echo "Existing installation detected / Instalação existente detectada"
  echo ""

  if [ "$MEETING_INSTALLED" = "yes" ]; then
    echo "Both modes are already installed / Ambos os modos já estão instalados"
    echo ""
    echo "  1) Reinstall from scratch / Reinstalar do zero"
    echo "  2) Exit / Sair"
    echo -n "Choice / Escolha [2]: "
    read -r existing_choice
    case "$existing_choice" in
      1) : ;;  # fall through to full install below
      *) echo "Nothing changed."; exit 0 ;;
    esac
  else
    echo "File transcription is installed / Transcrição de arquivo está instalada"
    echo ""
    echo "  1) Add real-time meeting support / Adicionar suporte a reunião em tempo real"
    echo "  2) Reinstall from scratch / Reinstalar do zero"
    echo "  3) Exit / Sair"
    echo -n "Choice / Escolha [1]: "
    read -r existing_choice

    case "$existing_choice" in
      2) : ;;  # fall through to full install below
      3) echo "Nothing changed."; exit 0 ;;
      *)
        # ── Add meeting support only ───────────────────────────────────────────
        echo ""
        if ! command -v brew &>/dev/null; then
          echo "Installing Homebrew / Instalando Homebrew..."
          /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        fi
        brew install portaudio blackhole-2ch
        echo "$(t setup.reloading_audio)"
        sudo killall coreaudiod || true

        source "$DIR/venv/bin/activate"
        pip install --upgrade pip --quiet
        pip install -r "$DIR/requirements-meeting.txt" --quiet

        # Update installed_modes in settings.json
        python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
path = sys.argv[1]
d = json.load(open(path))
modes = d.get("installed_modes", ["file"])
if "meeting" not in modes:
    modes.append("meeting")
d["installed_modes"] = modes
with open(path, "w") as f:
    json.dump(d, f, indent=2)
PYEOF

        echo ""
        echo "$(t setup.done)"
        echo ""
        print_audio_steps
        exit 0
        ;;
    esac
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Fresh install (or reinstall from scratch)
# ─────────────────────────────────────────────────────────────────────────────

# ── Language selection ────────────────────────────────────────────────────────
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

echo ""
echo "$(t setup.welcome)"
echo ""

# ── Use case selection ────────────────────────────────────────────────────────
echo "What do you need? / O que você precisa?"
echo "  1) Transcribe a video/audio file (default) / Transcrever um arquivo de vídeo/áudio"
echo "  2) Real-time meeting transcription / Transcrição de reunião em tempo real"
echo -n "Choice / Escolha [1]: "
read -r use_case_choice

case "$use_case_choice" in
  2) USE_CASE="meeting" ;;
  *) USE_CASE="file" ;;
esac

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

# ── Python interpreter selection (3.11-3.14) ─────────────────────────────────
echo ""
echo "Checking Python version / Verificando versão do Python..."

PYTHON=""
for candidate in python3.12 python3.11 python3.13 python3.14 python3; do
  if command -v "$candidate" &>/dev/null; then
    version=$("$candidate" -c 'import sys; print(sys.version_info[:2])')
    if "$candidate" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info < (3,15) else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      echo "  Using $candidate ($version)"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo ""
  echo "ERROR: Python 3.11 or newer is required but was not found."
  echo "ERRO: Python 3.11 ou superior é necessário mas não foi encontrado."
  echo ""
  echo "Install with Homebrew: brew install python@3.12"
  echo "Instale com Homebrew:  brew install python@3.12"
  exit 1
fi

# ── Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "Installing Homebrew / Instalando Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# ── System deps ───────────────────────────────────────────────────────────────
echo "$(t setup.installing_deps)"
brew install ffmpeg

if [ "$USE_CASE" = "meeting" ]; then
  brew install portaudio blackhole-2ch
  echo "$(t setup.reloading_audio)"
  sudo killall coreaudiod || true
fi

# ── Python venv ───────────────────────────────────────────────────────────────
echo "$(t setup.creating_venv)"
"$PYTHON" -m venv venv
source venv/bin/activate

echo "$(t setup.installing_packages)"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

if [ "$USE_CASE" = "meeting" ]; then
  pip install -r requirements-meeting.txt --quiet
fi

# ── Save settings.json ────────────────────────────────────────────────────────
python3 - "$UI_LANG" "$DIR/settings.json" "$FOLDER_STRUCTURE" "$USE_CASE" <<'PYEOF'
import json, sys
settings = {
    "ui_language": sys.argv[1],
    "folder_structure": sys.argv[3],
    "installed_modes": [sys.argv[4]],
    "setup_complete": True,
}
with open(sys.argv[2], "w") as f:
    json.dump(settings, f, indent=2)
PYEOF

echo ""
echo "$(t setup.done)"
echo ""

if [ "$USE_CASE" = "file" ]; then
  echo "To transcribe a file, run:"
  echo "  ./transcribe.sh <path/to/file.mp4>"
  echo ""
  echo "Para transcrever um arquivo, execute:"
  echo "  ./transcribe.sh <caminho/para/arquivo.mp4>"
  echo ""
  echo "To add real-time meeting support later, run ./setup.sh again."
  echo "Para adicionar suporte a reunião depois, rode ./setup.sh novamente."
else
  print_audio_steps
fi
