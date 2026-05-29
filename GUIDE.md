# Context Insights — Developer Guide

This project is a local demo for a standup-to-client-update pipeline built for Cloud++, a Brazilian dev outsourcing company. The idea: record a team standup, transcribe it, run it through Claude to generate a professional English client update, review it on a web platform, and send it to the client.

---

## What it does

```
Audio recording
      ↓
Whisper (large-v3) — speech-to-text + speaker diarization (pyannote)
      ↓
transcript.md — raw transcript with speaker labels
      ↓
Claude (claude CLI) — transforms transcript into client update
      ↓
client_update.md — professional English email update
      ↓
Web platform (localhost:5050) — review, edit, archive or send
```

---

## Prerequisites

- Python 3.11+
- [Claude CLI](https://claude.ai/code) installed and authenticated (`claude --version`)
- ffmpeg (`brew install ffmpeg`)
- For audio recording: BlackHole virtual audio device (macOS) — `brew install blackhole-2ch`
- For diarization: a Hugging Face token with access to pyannote models

Run the setup script once:
```bash
bash setup.sh
```

It creates a virtualenv and installs all dependencies. For diarization support:
```bash
bash setup.sh --diarize
```

---

## Running the web platform

```bash
venv/bin/python web/server.py
```

Open `http://localhost:5050`. Two sample transcripts (real standups) are already loaded in `web/data.json` so you can explore the UI immediately without recording anything.

---

## Key files

| File | Role |
|------|------|
| `web/server.py` | Flask API + static server, port 5050 |
| `web/index.html` | List view — pending updates, archived, explorer mode, Board |
| `web/update.html` | Review page — edit, compare raw transcript, send or archive |
| `web/board.html` | Board view — day list + full snapshot / brief side-by-side |
| `web/samples/boards/<date>/` | Daily board snapshots: `facts.json` (full) + `brief.json` |
| `web/app.js` | All frontend logic |
| `web/style.css` | Cloud++ design system styles |
| `web/data.json` | Local database — list of processed updates |
| `web/samples/` | Sample transcripts committed for demo purposes |
| `agents/client_update.md` | Prompt Claude uses to generate client updates |
| `agents/combined_update.md` | Prompt for merging multiple updates into one period summary |
| `agents/explore.md` | Prompt for the Explorer mode search feature |
| `process.py` | Runs `claude -p <agent> < <transcript>` and writes output |
| `transcribe_file.py` | Whisper transcription wrapper |
| `slack.sh` | End-to-end demo script: records audio → transcribes → submits to web |

---

## Web platform features

The list page is a **unified feed of Updates** — the living, client-facing record of the project. An *Update* is any unit of client-relevant project knowledge; today there are two types, each tagged with a badge and leading with the client-facing result:
- **Meeting** — a recording/transcript processed by Claude. Card title = the update subject; subtitle = meeting name · date. Click → `/update.html`.
- **Board** — a daily project-board snapshot (informational, auto-generated). Card title = the date; subtitle = board name · % complete · shipped count. Click → `/board.html?date=`.

Controls:
- **Source** tabs (`All · Meetings · Board`, left-most) — filter the feed by signal source. Board snapshots are informational, so under Source = Board the owner/status tabs are hidden.
- **My Updates / Team** tabs — filter meeting updates by owner.
- **Pending Review / Archived** tabs — filter meeting updates by status.
- **Import** dropdown — add an artifact: a **Recording** (audio/video, Whisper runs server-side) or a **Transcript** (paste/upload `.txt`/`.md`, Claude generates the update).
- **Generate Combined** — select 2+ meeting updates, generate a single consolidated period update.
- **Explorer mode** (sparkles button) — ask natural-language questions across all transcripts (e.g. "Why did we reject Feature X?").

The board detail page (`/board.html?date=<YYYY-MM-DD>`) shows the day's two files side by side: `facts.json` (full snapshot at end of day) on the left, `brief.json` (movement & insights) on the right — same split layout as the Raw transcript view.

### Board context for transcription
When a transcript is processed into a client update, the server looks for a board snapshot dated the same day (`web/samples/boards/<YYYY-MM-DD>/`). If found, `facts.json` + `brief.json` are passed to Claude as **optional reference context** to improve grounding and reduce hallucination. The board is never required — absent it, processing is unchanged.

### Review page (`/update.html?id=<id>`)
- Edit the generated update (auto-saved on every keystroke)
- **Raw** button — side-by-side view with the original transcript
- **Reprocess** — edit the raw transcript to fix transcription errors, optionally add instructions, re-run Claude
- **Archive** or **Send Email to Client**

---

## Adding a new update manually

Any audio or transcript file can be imported through the web UI. For a scripted flow (e.g. from a Slack bot or a scheduled job), POST to the API:

```python
import urllib.request, json

data = json.dumps({
    "meeting_name": "Sprint 14 standup",
    "content": "<generated update text>",
    "transcript_path": "/path/to/transcript.md"
}).encode()

req = urllib.request.Request(
    "http://localhost:5050/api/submit",
    data=data,
    headers={"Content-Type": "application/json"}
)
urllib.request.urlopen(req)
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/data` | List all updates |
| POST | `/api/submit` | Add an update directly |
| POST | `/api/save/<id>` | Update content (auto-save) |
| POST | `/api/send/<id>` | Mark as sent |
| POST | `/api/archive/<id>` | Mark as archived |
| POST | `/api/delete/<id>` | Delete permanently |
| GET | `/api/transcript/<id>` | Fetch raw transcript text |
| GET | `/api/boards` | List board snapshot days |
| GET | `/api/board/<date>` | Fetch a day's `facts` + `brief` JSON |
| POST | `/api/import/audio` | Upload audio file, run full pipeline |
| POST | `/api/import/transcript` | Submit transcript text, run Claude agent |
| POST | `/api/reprocess/<id>` | Re-run Claude on edited transcript |
| POST | `/api/generate-combined` | Combine selected updates into one |
| POST | `/api/explore` | Natural-language search across transcripts |
| GET | `/api/job/<job_id>` | Poll background job status |

---

## How the Claude agent works

`process.py` runs:
```bash
claude -p "$(cat agents/client_update.md)" < transcript.md > client_update.md
```

The agent files in `agents/` are plain markdown prompts. You can edit them to change tone, format, or language of the generated output.

---

## Architecture context (next steps beyond the demo)

This demo runs entirely local. The production roadmap:

| Component | Current (demo) | Future |
|-----------|---------------|--------|
| Recording | Local mic + BlackHole | [Recall.ai](https://www.recall.ai) bot joins Zoom/Meet/Teams |
| Transcription | Local Whisper large-v3 | AssemblyAI or Deepgram (~$0.01/min) |
| GPU | Developer machine | [Modal.com](https://modal.com) serverless GPU |
| Hosting | localhost | Railway or Render (~$10/mo) |
| Auth | None | Cloud++ SSO |
| Trigger | Manual script | Slack `/standup` slash command |

The **Board** view consumes daily snapshots of client project boards (`facts.json` + `brief.json` per day under `web/samples/boards/`). In the demo these files are committed; in production a scheduled job (Playwright scraping or board API) will generate them daily. The snapshots already feed the transcription pipeline as optional context (see *Board context for transcription* above), merging board state with standup context under the Context Insights umbrella.
