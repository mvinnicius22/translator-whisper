#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
NC=$'\033[0m'

if [ ! -f "$DIR/settings.json" ] || [ ! -d "$DIR/venv" ]; then
  echo "${RED}Setup not complete. Please run ./setup.sh first.${NC}"
  exit 1
fi

NOTES_INSTALLED=$(python3 - "$DIR/settings.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print("yes" if "notes" in d.get("installed_modes", []) else "no")
PYEOF
)

if [ "$NOTES_INSTALLED" != "yes" ]; then
  echo "${RED}Lecture notes not installed. Run ./setup.sh and choose the notes add-on.${NC}"
  exit 1
fi

source venv/bin/activate

# The entire interactive UI runs in Python to avoid complex shell array parsing.
# All display/prompts go to stderr (visible on terminal); only "__ARGS__:<json>" goes to stdout (captured).
OUTPUT=$(python - "$HOME/Documents/Meetings" <<'PYEOF'
import json, os, re, sys
from pathlib import Path

BOLD   = "\033[1m"
CYAN   = "\033[0;36m"
YELLOW = "\033[0;33m"
GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
NC     = "\033[0m"

def pr(s=""):
    print(s, flush=True, file=sys.stderr)

def ask(msg, default=""):
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
        val = sys.stdin.readline()
        if val == "":
            sys.exit(0)
        val = val.rstrip("\n").strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        sys.exit(0)

# ── Discover lecture sessions ──────────────────────────────────────────────────
# A "session" = a batch of clips 1..N transcribed together.
# Detection: sort clips by folder name (timestamp) within each (date, base_name);
# split into sessions when clip_num resets back toward 1.

meetings = Path(sys.argv[1])
if not meetings.exists():
    pr(f"{RED}Meetings folder not found: {meetings}{NC}")
    sys.exit(1)

# Map (date_dir_str, base) -> [(clip_num, folder_path)]
raw_groups: dict = {}
for transcript in sorted(meetings.rglob("transcript.md")):
    folder = transcript.parent
    name   = folder.name
    date   = str(folder.parent)

    m = re.search(r'-clipe-(\d+)$', name)
    if m:
        clip_num = int(m.group(1))
        base_raw = name[:m.start()]
        base = re.sub(r'^\d{4}_', '', base_raw)
    else:
        clip_num = 0
        base = re.sub(r'^\d{4}_', '', name)

    key = (date, base)
    raw_groups.setdefault(key, []).append((clip_num, folder))

# Sort each group by folder name (chronological), then split into sessions
sessions = []  # each entry: (date_dir, base, [folder, ...])
for (date_dir, base), clips in sorted(raw_groups.items(), key=lambda x: x[0], reverse=True):
    # Sort by folder name so batch order is chronological
    clips.sort(key=lambda x: x[1].name)

    group_sessions: list = []
    current: list = []
    prev_num = None
    for clip_num, folder in clips:
        if prev_num is not None and clip_num <= prev_num:
            # Clip number reset → previous batch ended
            group_sessions.append((date_dir, base, [f for f in current]))
            current = []
        current.append(folder)
        prev_num = clip_num
    if current:
        group_sessions.append((date_dir, base, current))
    # Newest batch first so "Enter = most recent" picks the latest transcription
    sessions.extend(reversed(group_sessions))

if not sessions:
    pr(f"{RED}No lectures found in {meetings}.{NC}")
    pr("Transcribe some audio files first with batch_transcribe.py or transcribe.sh.")
    sys.exit(1)

# ── Show menu ──────────────────────────────────────────────────────────────────
pr()
pr(f"{BOLD}{CYAN}Available lectures:{NC}")
pr()
for i, (date_dir, base, folders) in enumerate(sessions, 1):
    date_label = os.path.basename(date_dir)
    n = len(folders)
    # Detect if any transcript uses timestamps (for user awareness)
    has_ts = False
    for f in folders:
        t = (f / "transcript.md").read_text(encoding="utf-8")
        if re.search(r'\*\*\[\d{1,2}:\d{2}\]\*\*', t):
            has_ts = True
            break
    ts_label = "timestamped" if has_ts else "prose"
    pr(f"  {YELLOW}{i}){NC} {base}  [{date_label}, {n} clip{'s' if n>1 else ''}, {ts_label}]")

pr()
raw = ask(f"{BOLD}Select lecture (Enter for most recent): {NC}", "1")
try:
    idx = int(raw) - 1
    assert 0 <= idx < len(sessions)
except (ValueError, AssertionError):
    pr(f"{RED}Invalid selection.{NC}")
    sys.exit(1)

date_dir, base, folders = sessions[idx]
transcript_paths = [str(f / "transcript.md") for f in folders if (f / "transcript.md").exists()]

if not transcript_paths:
    pr(f"{RED}No transcript.md files found in selected session.{NC}")
    sys.exit(1)

pr()
pr(f"{GREEN}Selected: '{base}'  ({len(transcript_paths)} transcript(s)){NC}")

# ── Aux material ───────────────────────────────────────────────────────────────
pr()
aux_path = ask(f"{BOLD}Path to auxiliary PDF (Enter to skip): {NC}")
aux_arg: list = []
if aux_path:
    if os.path.isfile(aux_path):
        aux_arg = ["--aux", aux_path]
    else:
        pr(f"{YELLOW}File not found — skipping.{NC}")

# ── Mode ───────────────────────────────────────────────────────────────────────
pr()
pr(f"{BOLD}{CYAN}Options:{NC}")
pr(f"  {YELLOW}1){NC} Full run: agent + PDF  (default)")
pr(f"  {YELLOW}2){NC} Markdown only: stop before PDF (review before rendering)")
pr(f"  {YELLOW}3){NC} PDF only: re-render existing lecture_notes.md (skip agent)")
pr()
mode = ask(f"{BOLD}Choice [1]: {NC}", "1")
mode_flags: list = []
if mode == "2":
    mode_flags = ["--md-only"]
elif mode == "3":
    mode_flags = ["--skip-agent"]

# ── Emit args ──────────────────────────────────────────────────────────────────
result = {"transcripts": transcript_paths, "aux": aux_arg, "flags": mode_flags}
print("__ARGS__:" + json.dumps(result))
PYEOF
)

ARGS_JSON=$(echo "$OUTPUT" | grep '^__ARGS__:' | sed 's/^__ARGS__://')

if [ -z "$ARGS_JSON" ]; then
  exit 1
fi

# Build and run the command
CMD=$(python - "$ARGS_JSON" <<'PYEOF'
import json, shlex, sys
data = json.loads(sys.argv[1])
parts = ["python", "-u", "build_notes.py"]
parts += ["--transcripts"] + data["transcripts"]
parts += data["aux"]
parts += data["flags"]
print(" ".join(shlex.quote(p) for p in parts))
PYEOF
)

echo ""
eval "$CMD"
echo ""
echo "${GREEN}Done.${NC}"
