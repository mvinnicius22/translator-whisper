# Meeting Transcriber

Real-time meeting transcription for macOS using [OpenAI Whisper](https://github.com/openai/whisper). Captures both your microphone and system audio (via BlackHole), transcribes speech locally, and generates a Markdown document with the full dialogue at the end of the meeting.

## Requirements

- macOS (Apple Silicon recommended)
- [Homebrew](https://brew.sh)
- Python 3.9+

## Setup (run once)

```bash
./setup.sh
```

This will:
1. Ask your preferred interface language (English / Portuguese)
2. Install `ffmpeg`, `portaudio`, and `BlackHole 2ch` via Homebrew
3. Create a Python virtual environment and install dependencies
4. Save your preferences to `settings.json`
5. Print audio configuration instructions

## Audio configuration (run once)

After setup, configure a Multi-Output Device in **Audio MIDI Setup** so you can hear the meeting AND the app can capture it:

1. Open **Audio MIDI Setup** (Spotlight search)
2. Click `+` → **Create Multi-Output Device**
3. Check **MacBook Pro Speakers** + **BlackHole 2ch** (enable Drift Correction on BlackHole)
4. Go to **System Settings → Sound → Output** → select the new device
5. In **Google Meet → Settings → Audio**, leave Speaker as "System default"

## Usage

```bash
./run.sh
```

- Select the meeting language
- Press **ENTER** to start recording
- Join your meeting normally — the app runs silently in the terminal
- Press **Ctrl+C** to stop and save

The transcript is saved to `~/Documents/Meetings/meeting_YYYYMMDD_HHMMSS.md`.

## Output format

```markdown
# Meeting — 26/03/2026

## Info
| | |
|---|---|
| **Date** | 26/03/2026 |
| **Start** | 14:00 |
| **End** | 15:02 |
| **Duration** | 1h 2m 10s |
| **Participants** | You, Participant 1, Participant 2 |

## Transcript

**Participant 1** *(14:00:12)*: Hey everyone, can you hear me?
**You** *(14:00:15)*: Yes, loud and clear.
```

## How it works

- **Microphone** → transcribed and labeled as "You" (or "Você" in PT mode)
- **System audio** (via BlackHole) → transcribed and labeled as "Participant 1", "Participant 2", etc., using lightweight speaker identification based on spectral features
- Transcription runs fully **locally** using Whisper — no data leaves your machine
