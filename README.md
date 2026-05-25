# Meeting Transcriber

Transcribe video/audio files or capture real-time meeting audio, fully locally, using
[OpenAI Whisper](https://github.com/openai/whisper). No audio or text leaves your machine.

## Requirements

- macOS with Apple Silicon (Intel Macs are not supported: `torch==2.11.0` has no wheel for that architecture)
- [Homebrew](https://brew.sh)
- Python 3.11+
- [Claude CLI](https://claude.ai/code) (only needed for post-processing agents and prose formatting)

## Setup

```bash
./setup.sh
```

The setup asks what you need and installs only the relevant dependencies:

| Mode | System deps | Python deps |
|---|---|---|
| File transcription | `ffmpeg` | `openai-whisper`, `torch`, `numba`, `numpy`, `tqdm` |
| Real-time meeting | + `portaudio`, `BlackHole 2ch` | + `sounddevice` |

**Upgrading later:** if you installed file-only mode and later want real-time meeting
transcription, run `./setup.sh` again and choose "Add real-time meeting support".

## How it works

```
┌─────────────────────────────────────────────────────────┐
│                        setup.sh                         │
│  Detects existing install / fresh install               │
│  Installs system deps (brew) and Python deps (pip)      │
│  Writes settings.json (lang, folder structure, modes)   │
└────────────────┬────────────────────────────────────────┘
                 │
       ┌─────────┴──────────┐
       │                    │
       v                    v
┌─────────────┐     ┌──────────────────┐
│ FILE MODE   │     │  MEETING MODE    │
│             │     │                  │
│ transcribe  │     │ run.sh           │
│ .sh <file>  │     │                  │
│      │      │     │ app.py           │
│      v      │     │  captures mic +  │
│ transcribe  │     │  BlackHole audio │
│ _file.py    │     │      │           │
│  Whisper    │     │      v           │
│  (local)    │     │  Whisper         │
│      │      │     │  (local)         │
│      v      │     │      │           │
│ transcript  │     │      v           │
│ .md saved   │     │  transcript.md   │
└──────┬──────┘     └───────┬──────────┘
       │                    │
       └────────┬───────────┘
                │
                v
  ┌─────────────────────────────────────┐
  │  POST-PROCESSING (optional)         │
  │                                     │
  │  process.sh                         │
  │   select transcript + agent         │
  │        │                            │
  │        v                            │
  │  process.py                         │
  │   reads agents/<name>.md prompt     │
  │   calls: claude -p <prompt>         │
  │        │                            │
  │        v                            │
  │  <agent-name>.md saved alongside    │
  │  the transcript                     │
  └─────────────────────────────────────┘

  ┌─────────────────────────────────────┐
  │  PROSE FORMATTING (optional)        │
  │                                     │
  │  format_transcript.py <transcript>  │
  │   strips timestamps, deduplicates   │
  │   splits into 20-min chunks         │
  │   calls: claude -p <each chunk>     │
  │        │                            │
  │        v                            │
  │  transcript_formatted.md            │
  └─────────────────────────────────────┘
```

### Output location

Transcripts are saved to `~/Documents/Meetings/` in one of two structures:

| Setting | Example path |
|---|---|
| Flat (default) | `~/Documents/Meetings/20260525_1430_project-kickoff/transcript.md` |
| Daily | `~/Documents/Meetings/20260525/1430_project-kickoff/transcript.md` |

Choose the structure during `./setup.sh`. You can change it by editing `settings.json`.

### Codebase overview

| File | Role |
|---|---|
| `setup.sh` | One-time setup: installs deps, creates venv, writes `settings.json` |
| `transcribe.sh` | Entry point for file transcription |
| `run.sh` | Entry point for real-time meeting transcription |
| `process.sh` | Entry point for post-processing a saved transcript with an agent |
| `transcribe_file.py` | Core file transcription logic (Whisper, progress bar, markdown output) |
| `app.py` | Core real-time transcription logic (audio capture, VAD, diarization) |
| `process.py` | Runs a selected agent prompt via `claude -p` and saves the output |
| `format_transcript.py` | Optional prose formatter: strips timestamps, polishes text via Claude |
| `folders.py` | Shared helper: computes the output folder path from `settings.json` |

## Transcribe a file

```bash
./transcribe.sh path/to/video.mp4
```

Or run without arguments to be prompted:

```bash
./transcribe.sh
```

You will be asked:
- **Model**: `medium` (~1.5 GB download on first use) or `large` (~3 GB, best accuracy)
- **Language**: audio language code, e.g. `pt`, `en`, `es`
- **Format**: `timestamped` or `prose` (see below)
- **Output folder name**: defaults to the filename

### Output formats

| Format | Description | Best for |
|---|---|---|
| `timestamped` (default) | Each Whisper segment on its own line, prefixed with `[MM:SS]` | Navigation, reference, post-editing |
| `prose` | All text joined into continuous paragraphs, no timestamps | Reading, sharing, copy-paste |

**Note:** file transcription does not identify who is speaking. All audio becomes continuous
text regardless of how many speakers are present. If you need speaker labels, see the
[prose formatting](#prose-formatting-optional) section or wait for the `--diarize` flag
(planned, not yet implemented).

You can also pass `--format` directly when calling the script:

```bash
venv/bin/python transcribe_file.py --file video.mp4 --lang en --format prose
venv/bin/python transcribe_file.py --file video.mp4 --lang en --format timestamped
```

## Real-time meeting transcription

### Audio setup (one-time, after `./setup.sh` with meeting mode)

1. Open **Audio MIDI Setup** (Spotlight search)
2. Click `+` > **Create Multi-Output Device**
3. Check **your Mac's speakers (or headphones)** + **BlackHole 2ch** (enable Drift Correction on BlackHole)
4. **System Settings > Sound > Output** > select the new Multi-Output Device
5. In your meeting app (Google Meet, Zoom, etc.), leave Speaker as "System default"

### Start a meeting

```bash
./run.sh
```

Select language, press ENTER to start recording, and Ctrl+C to stop and save.

## Post-processing (optional)

After any transcription, run an agent to produce a summary, action items, etc.:

```bash
./process.sh
```

The script presents two menus in sequence:

1. **Select a transcript**: lists the 20 most recent folders under `~/Documents/Meetings/`
   that contain a `transcript.md`
2. **Select an agent**: lists all `.md` files found in the `agents/` folder

After both selections, `process.py` reads the transcript and the agent prompt, calls
`claude -p "<prompt>\n\n<transcript>"`, and saves the output as `<agent-name>.md` in the
same folder as the transcript.

**Prerequisite:** the [Claude CLI](https://claude.ai/code) must be installed and authenticated.

### Available agents

| Agent | Output |
|---|---|
| `summary` | Full meeting summary with key decisions |
| `actions` | Complete action item register with owners |
| `next_steps` | Checklist of every next step and commitment |
| `topics` | All topics discussed with key points |
| `feedback_session` | Structured output for 1:1 / performance reviews |

### Adding a custom agent

Create a new `.md` file in the `agents/` folder with your prompt. It will appear
automatically in the `./process.sh` menu. The transcript is appended to your prompt and
sent to Claude.

## Prose formatting (optional)

`format_transcript.py` uses Claude to rewrite the raw Whisper output as polished prose:
corrects names, fixes grammar, and restructures the text. This is different from `--format prose`
(which only removes timestamps and groups sentences into paragraphs without any AI rewriting).
Designed for Portuguese transcripts. Requires the Claude CLI.

```bash
venv/bin/python format_transcript.py path/to/transcript.md
# Output: path/to/transcript_formatted.md
```
