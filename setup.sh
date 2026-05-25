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
  echo "${BOLD}${CYAN}══════════════════════════════════════════════════════${NC}"
  echo "${BOLD}  $(t setup.audio_title)${NC}"
  echo "${BOLD}${CYAN}══════════════════════════════════════════════════════${NC}"
  echo ""
  t setup.audio_step1; echo ""
  t setup.audio_step2; echo ""
  t setup.audio_step3; echo ""
  t setup.audio_step4; echo ""
  t setup.audio_step5; echo ""
  t setup.audio_step6; echo ""
  echo "${BOLD}${CYAN}══════════════════════════════════════════════════════${NC}"
  echo "${BOLD}  $(t setup.how_to_use_title)${NC}"
  echo "${BOLD}${CYAN}══════════════════════════════════════════════════════${NC}"
  echo ""
  t setup.how_to_use; echo ""
}

# ── Detect existing installation ──────────────────────────────────────────────
if [ -d "$DIR/venv" ] && [ -f "$DIR/settings.json" ]; then

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
  echo "${BOLD}${CYAN}Existing installation detected / Instalação existente detectada${NC}"
  echo ""

  if [ "$MEETING_INSTALLED" = "yes" ]; then
    echo "${GREEN}Both modes are already installed / Ambos os modos já estão instalados${NC}"
    echo ""
    echo "  ${YELLOW}1)${NC} Reinstall from scratch / Reinstalar do zero"
    echo "  ${YELLOW}2)${NC} Exit / Sair"
    echo ""
    echo -n "${BOLD}Choice / Escolha [2]: ${NC}"
    read -r existing_choice
    case "$existing_choice" in
      1) : ;;
      *) echo "Nothing changed."; exit 0 ;;
    esac
  else
    echo "${CYAN}File transcription is installed / Transcrição de arquivo está instalada${NC}"
    echo ""
    echo "  ${YELLOW}1)${NC} Add real-time meeting support / Adicionar suporte a reunião em tempo real"
    echo "  ${YELLOW}2)${NC} Reinstall from scratch / Reinstalar do zero"
    echo "  ${YELLOW}3)${NC} Exit / Sair"
    echo ""
    echo -n "${BOLD}Choice / Escolha [1]: ${NC}"
    read -r existing_choice

    case "$existing_choice" in
      2) : ;;
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
        echo "${GREEN}$(t setup.done)${NC}"
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
echo "${BOLD}${CYAN}Choose interface language / Escolha o idioma da interface:${NC}"
echo "  ${YELLOW}1)${NC} English (default)"
echo "  ${YELLOW}2)${NC} Português"
echo ""
echo -n "${BOLD}Choice / Escolha [1]: ${NC}"
read -r lang_choice

case "$lang_choice" in
  2) UI_LANG="pt" ;;
  *) UI_LANG="en" ;;
esac

echo ""
echo "${BOLD}${CYAN}$(t setup.welcome)${NC}"
echo ""

# ── Use case selection ────────────────────────────────────────────────────────
echo "${BOLD}What do you need? / O que você precisa?${NC}"
echo "  ${YELLOW}1)${NC} Transcribe a video/audio file / Transcrever arquivo de vídeo/áudio  ${BOLD}(default)${NC}"
echo "  ${YELLOW}2)${NC} Real-time meeting transcription / Transcrição de reunião em tempo real"
echo "  ${YELLOW}3)${NC} Both / Ambos"
echo ""
echo -n "${BOLD}Choice / Escolha [1]: ${NC}"
read -r use_case_choice

case "$use_case_choice" in
  2) USE_CASE="meeting" ;;
  3) USE_CASE="both" ;;
  *) USE_CASE="file" ;;
esac

# ── Folder structure selection ────────────────────────────────────────────────
echo ""
echo "${BOLD}${CYAN}$(t setup.folder_structure_title)${NC}"
t setup.folder_structure_options
echo ""
echo -n "${BOLD}$(t setup.folder_structure_prompt)${NC}"
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
  echo "${RED}ERROR: Python 3.11 or newer is required but was not found.${NC}"
  echo "${RED}ERRO: Python 3.11 ou superior é necessário mas não foi encontrado.${NC}"
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

if [ "$USE_CASE" = "file" ] || [ "$USE_CASE" = "both" ]; then
  brew install ffmpeg
fi

if [ "$USE_CASE" = "meeting" ] || [ "$USE_CASE" = "both" ]; then
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

if [ "$USE_CASE" = "meeting" ] || [ "$USE_CASE" = "both" ]; then
  pip install -r requirements-meeting.txt --quiet
fi

# ── Save settings.json ────────────────────────────────────────────────────────
python3 - "$UI_LANG" "$DIR/settings.json" "$FOLDER_STRUCTURE" "$USE_CASE" <<'PYEOF'
import json, sys
use_case = sys.argv[4]
modes = ["file", "meeting"] if use_case == "both" else [use_case]
settings = {
    "ui_language": sys.argv[1],
    "folder_structure": sys.argv[3],
    "installed_modes": modes,
    "setup_complete": True,
}
with open(sys.argv[2], "w") as f:
    json.dump(settings, f, indent=2)
PYEOF

echo ""
echo "${GREEN}$(t setup.done)${NC}"
echo ""

if [ "$USE_CASE" = "file" ]; then
  echo "To transcribe a file, run:"
  echo "  ${BOLD}./transcribe.sh <path/to/file.mp4>${NC}"
  echo ""
  echo "Para transcrever um arquivo, execute:"
  echo "  ${BOLD}./transcribe.sh <caminho/para/arquivo.mp4>${NC}"
  echo ""
  echo "To add real-time meeting support later, run ./setup.sh again."
  echo "Para adicionar suporte a reunião depois, rode ./setup.sh novamente."
elif [ "$USE_CASE" = "both" ]; then
  echo "To transcribe a file: ${BOLD}./transcribe.sh${NC}"
  echo ""
  print_audio_steps
else
  print_audio_steps
fi
