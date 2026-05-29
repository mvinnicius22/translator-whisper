#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
CYAN=$'\033[0;36m'
RED=$'\033[0;31m'
NC=$'\033[0m'

usage() {
  echo "Usage: ./lecture.sh [--file path]... [--folder path] --name label [--lang code] [--aux pdf] [--skip-transcription]"
  echo ""
  echo "  --file path           Audio clip (repeat for each clip, in chronological order)"
  echo "  --folder path         Folder with audio clips (all formats, sorted by filename)"
  echo "  --name label          Lecture name; PDF saved as <label>.pdf (default: first file's stem)"
  echo "  --lang code           Audio language code (default: pt)"
  echo "  --aux path            Auxiliary PDF (slides/handout)"
  echo "  --skip-transcription  Re-use transcripts already saved in <audio-folder>/transcripts/"
  exit 1
}

FILES=()
FOLDER=""
NAME=""
LANG="pt"
AUX_PATH=""
SKIP_TRANSCRIPTION=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)               FILES+=("$2"); shift 2 ;;
    --folder)             FOLDER="$2";   shift 2 ;;
    --name)               NAME="$2";     shift 2 ;;
    --lang)               LANG="$2";     shift 2 ;;
    --aux)                AUX_PATH="$2"; shift 2 ;;
    --skip-transcription) SKIP_TRANSCRIPTION=1; shift ;;
    -h|--help) usage ;;
    *) echo "${RED}Unknown option: $1${NC}" >&2; usage ;;
  esac
done

# Collect files from folder, sorted by filename
if [[ -n "$FOLDER" ]]; then
  [[ ! -d "$FOLDER" ]] && { echo "${RED}Error: folder not found: $FOLDER${NC}" >&2; exit 1; }
  while IFS= read -r -d $'\0' f; do
    FILES+=("$f")
  done < <(find "$FOLDER" -maxdepth 1 -type f \
    \( -iname "*.m4a" -o -iname "*.mp3" -o -iname "*.mp4" -o -iname "*.wav" \
       -o -iname "*.aac" -o -iname "*.mov" -o -iname "*.mkv" \
       -o -iname "*.flac" -o -iname "*.ogg" \) -print0 | sort -z)
fi

[[ ${#FILES[@]} -eq 0 ]] && { echo "${RED}Error: no audio files specified.${NC}" >&2; usage; }

[[ -z "$NAME" ]] && NAME="$(basename "${FILES[0]%.*}")"

SOURCE_DIR="$(cd "$(dirname "${FILES[0]}")" && pwd)"

source "$DIR/venv/bin/activate"

TRANSCRIPTS_DIR="$SOURCE_DIR/transcripts"
mkdir -p "$TRANSCRIPTS_DIR"

_fmt_secs() {
  local s=$1
  [[ $s -ge 60 ]] && printf "%dm%02ds" $((s/60)) $((s%60)) || printf "%ds" "$s"
}

# ── Header ──────────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}${CYAN}Lecture Pipeline${NC}"
echo ""
printf "  Name   : ${BOLD}%s${NC}\n"       "$NAME"
printf "  Clips  : %d\n"                   "${#FILES[@]}"
printf "  Lang   : %s\n"                   "$LANG"
[[ -n "$AUX_PATH" ]] && printf "  Aux    : %s\n" "$(basename "$AUX_PATH")"
printf "  Output : %s/${BOLD}%s.pdf${NC}\n" "$SOURCE_DIR" "$NAME"
echo ""

WALL_START=$(date +%s)
TOTAL_TRANSCRIPT_SECS=0
TRANSCRIPT_PATHS=()

# ── Stage 1: Transcription ───────────────────────────────────────────────────
if [[ $SKIP_TRANSCRIPTION -eq 1 ]]; then
  echo "${BOLD}▶ Stage 1 — Transcription (skipped)${NC}"
  for i in "${!FILES[@]}"; do
    tp="$TRANSCRIPTS_DIR/clip$((i+1))/transcript.md"
    [[ ! -f "$tp" ]] && { echo "${RED}Error: transcript not found: $tp${NC}" >&2; exit 1; }
    TRANSCRIPT_PATHS+=("$tp")
  done
  printf "  Using existing transcripts in %s/transcripts/\n\n" "$SOURCE_DIR"
else
  echo "${BOLD}▶ Stage 1 — Transcription${NC}"

  for i in "${!FILES[@]}"; do
    f="${FILES[$i]}"
    clip_dir="$TRANSCRIPTS_DIR/clip$((i+1))"
    mkdir -p "$clip_dir"

    printf "\n  [%d/%d] %s\n" $((i+1)) "${#FILES[@]}" "$(basename "$f")"

    t0=$(date +%s)
    python transcribe_file.py \
      --file "$f" \
      --model large-v3 \
      --lang "$LANG" \
      --format timestamped \
      --meeting-name "$NAME" \
      --output-folder "$clip_dir"
    t1=$(date +%s)
    clip_secs=$((t1 - t0))
    TOTAL_TRANSCRIPT_SECS=$((TOTAL_TRANSCRIPT_SECS + clip_secs))
    TRANSCRIPT_PATHS+=("$clip_dir/transcript.md")
    printf "  ${GREEN}✓${NC} $(_fmt_secs $clip_secs)\n"
  done

  echo ""
  printf "  Transcription total: ${BOLD}$(_fmt_secs $TOTAL_TRANSCRIPT_SECS)${NC}\n"
  echo ""
fi

# ── Stage 2: Assemble + agent ────────────────────────────────────────────────
echo "${BOLD}▶ Stage 2 — Generating lecture notes (Claude)${NC}"

AUX_ARG=()
[[ -n "$AUX_PATH" ]] && AUX_ARG=(--aux "$AUX_PATH")

t0=$(date +%s)
python build_notes.py \
  --transcripts "${TRANSCRIPT_PATHS[@]}" \
  --output-dir "$SOURCE_DIR" \
  --md-only \
  "${AUX_ARG[@]}"
t1=$(date +%s)
AGENT_SECS=$((t1 - t0))

# Token estimate from file sizes (≈4 chars/token for PT)
INPUT_CHARS=$(wc -c < "$DIR/agents/lecture_notes.md")
for tp in "${TRANSCRIPT_PATHS[@]}"; do
  INPUT_CHARS=$((INPUT_CHARS + $(wc -c < "$tp")))
done
OUTPUT_CHARS=$(wc -c < "$SOURCE_DIR/lecture_notes.md")
INPUT_TOK=$((INPUT_CHARS / 4))
OUTPUT_TOK=$((OUTPUT_CHARS / 4))
COST=$(python3 -c "i=$INPUT_TOK; o=$OUTPUT_TOK; print(f'{(i*3.0 + o*15.0)/1000000:.3f}')")

printf "  ${GREEN}✓${NC} $(_fmt_secs $AGENT_SECS)\n"
printf "  Tokens — input ~%dk, output ~%dk  (≈ \$%s)\n" \
  $((INPUT_TOK/1000)) $((OUTPUT_TOK/1000)) "$COST"
echo ""

# ── Stage 3: PDF render ───────────────────────────────────────────────────────
echo "${BOLD}▶ Stage 3 — Rendering PDF${NC}"

t0=$(date +%s)
python render_notes.py --md "$SOURCE_DIR/lecture_notes.md"
t1=$(date +%s)
RENDER_SECS=$((t1 - t0))

[[ -f "$SOURCE_DIR/lecture_notes.pdf" ]] && \
  mv "$SOURCE_DIR/lecture_notes.pdf" "$SOURCE_DIR/${NAME}.pdf"

printf "  ${GREEN}✓${NC} $(_fmt_secs $RENDER_SECS)\n"
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
WALL_SECS=$(( $(date +%s) - WALL_START ))

echo "${BOLD}${CYAN}── Summary ─────────────────────────────────────────────${NC}"
printf "  Transcription : %s\n"      "$(_fmt_secs $TOTAL_TRANSCRIPT_SECS)"
printf "  Agent         : %s\n"      "$(_fmt_secs $AGENT_SECS)"
printf "  PDF render    : %s\n"      "$(_fmt_secs $RENDER_SECS)"
printf "  ${BOLD}Total         : %s${NC}\n" "$(_fmt_secs $WALL_SECS)"
printf "  Tokens        : ~%dk in, ~%dk out  (≈ \$%s)\n" \
  $((INPUT_TOK/1000)) $((OUTPUT_TOK/1000)) "$COST"
echo ""
printf "  ${GREEN}PDF:${NC}        %s/%s.pdf\n" "$SOURCE_DIR" "$NAME"
[[ -f "$SOURCE_DIR/flashcards.csv" ]] && \
  printf "  ${GREEN}Flashcards:${NC} %s/flashcards.csv\n" "$SOURCE_DIR"
echo ""
