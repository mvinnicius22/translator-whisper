#!/usr/bin/env python3
import json
import re
import shutil
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# Make parent project importable
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

app = Flask(__name__, static_folder=".", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

DATA_FILE = Path(__file__).parent / "data.json"
PORT = 5050
AGENT_FILE          = _ROOT / "agents" / "client_update.md"
COMBINED_AGENT_FILE = _ROOT / "agents" / "combined_update.md"
EXPLORE_AGENT_FILE  = _ROOT / "agents" / "explore.md"
BOARDS_DIR          = Path(__file__).parent / "samples" / "boards"
_DATE_RE            = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_jobs: dict[str, dict] = {}


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_data() -> list:
    if not DATA_FILE.exists():
        return []
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def save_data(updates: list) -> None:
    DATA_FILE.write_text(json.dumps(updates, indent=2, ensure_ascii=False), encoding="utf-8")


def find_update(updates: list, uid: str) -> dict | None:
    return next((u for u in updates if u["id"] == uid), None)


def resolve_transcript(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else _ROOT / p


# ── Board helpers ───────────────────────────────────────────────────────────

def find_board(date: str) -> dict | None:
    """Return {facts: Path, brief: Path|None} for a board day, or None."""
    if not _DATE_RE.match(date or ""):
        return None
    day_dir = BOARDS_DIR / date
    facts = day_dir / "facts.json"
    if not facts.exists():
        return None
    brief = day_dir / "brief.json"
    return {"facts": facts, "brief": brief if brief.exists() else None}


def _build_board_context(date: str) -> str | None:
    """Raw full + brief board text for a day, used as optional agent context."""
    board = find_board(date)
    if not board:
        return None
    parts = ["FULL BOARD SNAPSHOT (facts.json):", board["facts"].read_text(encoding="utf-8")]
    if board["brief"]:
        parts += ["", "BOARD BRIEF (brief.json):", board["brief"].read_text(encoding="utf-8")]
    return "\n".join(parts)


def _build_board_digest(date: str) -> str | None:
    """Compact, reference-bearing board summary for the explorer.

    Keeps the fields the agent cites (id, title, status, assignee, due,
    blocked_by/blocks, url + brief client message/attention) and drops the
    bulky redundant blocks (history, movements_enriched, by_status, etc.) so
    the prompt — and thus the response — stays fast.
    """
    board = find_board(date)
    if not board:
        return None
    try:
        facts = json.loads(board["facts"].read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return _build_board_context(date)  # fall back to raw on parse failure

    due_by = {o["id"]: o.get("due_date") for o in facts.get("overdue", [])}
    t = facts.get("totals", {})
    lines = [
        f"Board: {facts.get('board', 'Project Board')} ({date}) — "
        f"{t.get('pct', '?')}% complete, {t.get('done_tickets', '?')}/{t.get('tickets', '?')} tickets done.",
        "",
        "CARDS (id | title | status | assignee | type | due | blocked_by | blocks | url):",
    ]
    for c in facts.get("cards", []):
        lines.append(
            " | ".join([
                c.get("id", "?"),
                c.get("title", ""),
                c.get("status", ""),
                c.get("assignee") or "unassigned",
                c.get("type", ""),
                due_by.get(c.get("id"), "") or "",
                ",".join(c.get("blocked_by", [])) or "-",
                ",".join(c.get("blocks", [])) or "-",
                c.get("url", ""),
            ])
        )

    overdue = facts.get("overdue", [])
    if overdue:
        lines += ["", "OVERDUE:"]
        lines += [f"- {o['id']} \"{o.get('title','')}\" ({o.get('status','')}, due {o.get('due_date','')})"
                  for o in overdue]

    if board["brief"]:
        try:
            brief = json.loads(board["brief"].read_text(encoding="utf-8"))
            if brief.get("client_message"):
                lines += ["", "CLIENT BRIEF:", brief["client_message"]]
            attn = brief.get("attention", [])
            if attn:
                lines += ["", "NEEDS ATTENTION:"]
                lines += [f"- {a.get('title','')}: {a.get('detail','')}" for a in attn]
        except (ValueError, OSError):
            pass

    return "\n".join(lines)


def _insert_update(meeting_name: str, content: str, transcript_path: str,
                   session_start: datetime, owner: str = "me") -> str:
    updates = load_data()
    uid = str(uuid.uuid4())[:8]
    updates.insert(0, {
        "id": uid,
        "meeting_name": meeting_name,
        "date": session_start.strftime("%Y-%m-%d"),
        "created_at": session_start.isoformat(),
        "content": content,
        "status": "pending",
        "owner": owner,
        "transcript_path": transcript_path,
    })
    save_data(updates)
    return uid


# ── Background jobs ───────────────────────────────────────────────────────────

def _run_audio_job(job_id: str, audio_path: Path, meeting_name: str, language: str):
    try:
        _jobs[job_id] = {"status": "processing", "step": "Transcribing audio…"}

        from transcribe_file import transcribe_file as _tf
        from folders import get_meeting_folder

        session_start = datetime.now()
        folder = get_meeting_folder(meeting_name or "import", session_start)
        folder.mkdir(parents=True, exist_ok=True)

        dest = folder / audio_path.name
        shutil.copy2(audio_path, dest)

        transcript_path = _tf(
            str(dest),
            model_size="large-v3",
            language=language,
            meeting_name=meeting_name,
            fmt="prose",
            diarize=True,
            output_folder=folder,
            session_start=session_start,
        )

        if not transcript_path or not Path(transcript_path).exists():
            raise RuntimeError("Transcription produced no output")

        _jobs[job_id]["step"] = "Generating client update…"

        output_path = folder / "client_update.md"
        from process import run_agent
        board_context = _build_board_context(session_start.strftime("%Y-%m-%d"))
        run_agent(str(transcript_path), str(AGENT_FILE), str(output_path),
                  board_context=board_context)

        content = output_path.read_text(encoding="utf-8")
        uid = _insert_update(
            meeting_name or folder.name,
            content,
            str(transcript_path),
            session_start,
        )
        _jobs[job_id] = {"status": "done", "update_id": uid}

    except Exception as exc:
        _jobs[job_id] = {"status": "error", "message": str(exc)}
    finally:
        audio_path.unlink(missing_ok=True)


def _run_transcript_job(job_id: str, transcript_text: str, meeting_name: str):
    try:
        _jobs[job_id] = {"status": "processing", "step": "Generating client update…"}

        from folders import get_meeting_folder

        session_start = datetime.now()
        folder = get_meeting_folder(meeting_name or "import", session_start)
        folder.mkdir(parents=True, exist_ok=True)

        transcript_path = folder / "transcript.md"
        transcript_path.write_text(transcript_text, encoding="utf-8")

        output_path = folder / "client_update.md"
        from process import run_agent
        board_context = _build_board_context(session_start.strftime("%Y-%m-%d"))
        run_agent(str(transcript_path), str(AGENT_FILE), str(output_path),
                  board_context=board_context)

        content = output_path.read_text(encoding="utf-8")
        uid = _insert_update(
            meeting_name or folder.name,
            content,
            str(transcript_path),
            session_start,
        )
        _jobs[job_id] = {"status": "done", "update_id": uid}

    except Exception as exc:
        _jobs[job_id] = {"status": "error", "message": str(exc)}


def _run_combined_job(job_id: str, selected_updates: list, name: str):
    try:
        _jobs[job_id] = {"status": "processing", "step": "Generating combined update…"}

        parts = []
        for i, u in enumerate(selected_updates, 1):
            parts.append(
                f"--- Update {i}: {u['meeting_name']} ({u['date']}) ---\n\n{u['content']}"
            )
        combined_text = "\n\n".join(parts)

        from datetime import datetime
        from folders import get_meeting_folder
        from process import run_agent

        session_start = datetime.now()
        folder = get_meeting_folder(name or "combined", session_start)
        folder.mkdir(parents=True, exist_ok=True)

        input_path = folder / "combined_input.md"
        input_path.write_text(combined_text, encoding="utf-8")

        output_path = folder / "combined_update.md"
        run_agent(str(input_path), str(COMBINED_AGENT_FILE), str(output_path))

        content = output_path.read_text(encoding="utf-8")
        uid = _insert_update(
            name or f"Combined — {len(selected_updates)} updates",
            content,
            str(input_path),
            session_start,
        )
        _jobs[job_id] = {"status": "done", "update_id": uid}

    except Exception as exc:
        _jobs[job_id] = {"status": "error", "message": str(exc)}


def _run_reprocess_job(job_id: str, uid: str, transcript_text: str, hint: str):
    try:
        _jobs[job_id] = {"status": "processing", "step": "Generating client update…"}

        updates = load_data()
        update = find_update(updates, uid)
        if not update:
            raise RuntimeError("Update not found")

        transcript_path = update.get("transcript_path", "")
        if transcript_path and Path(transcript_path).exists():
            Path(transcript_path).write_text(transcript_text, encoding="utf-8")
        else:
            from folders import get_meeting_folder
            session_start = datetime.now()
            folder = get_meeting_folder(update.get("meeting_name", "reprocess"), session_start)
            folder.mkdir(parents=True, exist_ok=True)
            transcript_path = str(folder / "transcript.md")
            Path(transcript_path).write_text(transcript_text, encoding="utf-8")
            update["transcript_path"] = transcript_path
            save_data(updates)

        input_text = transcript_text
        if hint:
            input_text = f"## Reviewer instructions:\n{hint}\n\n## Transcript:\n{transcript_text}"

        import tempfile
        tmp_input = Path(tempfile.mktemp(suffix=".md"))
        tmp_input.write_text(input_text, encoding="utf-8")

        output_path = Path(transcript_path).parent / "client_update.md"
        from process import run_agent
        run_agent(str(tmp_input), str(AGENT_FILE), str(output_path))
        tmp_input.unlink(missing_ok=True)

        content = output_path.read_text(encoding="utf-8")

        updates = load_data()
        update = find_update(updates, uid)
        if update:
            update["content"] = content
            save_data(updates)

        _jobs[job_id] = {"status": "done", "update_id": uid}
    except Exception as exc:
        _jobs[job_id] = {"status": "error", "message": str(exc)}


def _run_explore_job(job_id: str, query: str):
    try:
        _jobs[job_id] = {"status": "processing", "step": "Searching transcripts & boards…"}

        updates = load_data()
        parts = []
        for u in updates:
            tp = u.get("transcript_path", "")
            if tp and Path(tp).exists():
                text = Path(tp).read_text(encoding="utf-8")
                parts.append(
                    f"=== TRANSCRIPT — Meeting: {u['meeting_name']} ({u['date']}) ===\n\n{text}"
                )

        if BOARDS_DIR.exists():
            for day_dir in sorted(BOARDS_DIR.iterdir()):
                if not day_dir.is_dir():
                    continue
                ctx = _build_board_digest(day_dir.name)
                if ctx:
                    parts.append(
                        f"=== PROJECT BOARD ({day_dir.name}) ===\n\n{ctx}"
                    )

        if not parts:
            _jobs[job_id] = {"status": "done", "answer": "No transcripts or boards available to search."}
            return

        import tempfile
        tmp_input = Path(tempfile.mktemp(suffix=".md"))
        tmp_input.write_text(f"QUESTION: {query}\n\n---\n\n" + "\n\n".join(parts), encoding="utf-8")

        tmp_output = Path(tempfile.mktemp(suffix=".md"))
        from process import run_agent
        run_agent(str(tmp_input), str(EXPLORE_AGENT_FILE), str(tmp_output), model="haiku")
        tmp_input.unlink(missing_ok=True)

        answer = tmp_output.read_text(encoding="utf-8") if tmp_output.exists() else "No answer found."
        tmp_output.unlink(missing_ok=True)

        _jobs[job_id] = {"status": "done", "answer": answer}
    except Exception as exc:
        _jobs[job_id] = {"status": "error", "message": str(exc)}


def _start_job(target, *args) -> str:
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "queued"}
    t = threading.Thread(target=target, args=(job_id, *args), daemon=True)
    t.start()
    return job_id


# ── Static routes ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return send_from_directory(Path(__file__).parent, "index.html")


@app.get("/update.html")
def update_page():
    return send_from_directory(Path(__file__).parent, "update.html")


@app.get("/board.html")
def board_page():
    return send_from_directory(Path(__file__).parent, "board.html")


@app.get("/api/boards")
def list_boards():
    boards = []
    if BOARDS_DIR.exists():
        for day_dir in BOARDS_DIR.iterdir():
            if not (day_dir.is_dir() and _DATE_RE.match(day_dir.name)):
                continue
            facts_file = day_dir / "facts.json"
            if not facts_file.exists():
                continue
            entry = {"date": day_dir.name, "has_brief": (day_dir / "brief.json").exists()}
            try:
                facts = json.loads(facts_file.read_text(encoding="utf-8"))
                entry["board"] = facts.get("board")
                entry["pct"] = (facts.get("totals") or {}).get("pct")
                entry["delivered_count"] = facts.get("delivered_count")
            except (json.JSONDecodeError, OSError):
                pass
            boards.append(entry)
    boards.sort(key=lambda b: b["date"], reverse=True)
    return jsonify(boards)


@app.get("/api/board/<date>")
def get_board(date):
    board = find_board(date)
    if not board:
        return jsonify({"error": "Not found"}), 404
    facts = json.loads(board["facts"].read_text(encoding="utf-8"))
    brief = json.loads(board["brief"].read_text(encoding="utf-8")) if board["brief"] else None
    return jsonify({"date": date, "facts": facts, "brief": brief})


@app.get("/api/transcript/<uid>")
def get_transcript(uid):
    updates = load_data()
    update = find_update(updates, uid)
    if not update:
        return jsonify({"error": "Not found"}), 404
    path = update.get("transcript_path", "")
    if not path:
        return jsonify({"content": None})
    resolved = resolve_transcript(path)
    if not resolved.exists():
        return jsonify({"content": None})
    return jsonify({"content": resolved.read_text(encoding="utf-8")})


@app.get("/health")
def health():
    return jsonify({"ok": True})


# ── Data API ──────────────────────────────────────────────────────────────────

@app.get("/api/data")
def get_data():
    return jsonify(load_data())


@app.post("/api/submit")
def submit():
    data = request.get_json(force=True)
    session_start = datetime.now()
    uid = _insert_update(
        data.get("meeting_name", "Untitled"),
        data.get("content", ""),
        data.get("transcript_path", ""),
        session_start,
    )
    return jsonify({"id": uid, "url": f"http://localhost:{PORT}/update.html?id={uid}"})


@app.post("/api/save/<uid>")
def save_update(uid):
    data = request.get_json(force=True)
    updates = load_data()
    update = find_update(updates, uid)
    if update:
        update["content"] = data.get("content", update["content"])
        save_data(updates)
    return jsonify({"ok": True})


@app.post("/api/send/<uid>")
def send_update(uid):
    updates = load_data()
    update = find_update(updates, uid)
    if update:
        update["status"] = "sent"
        update["sent_at"] = datetime.now().isoformat()
        save_data(updates)
    return jsonify({"ok": True})


@app.post("/api/delete/<uid>")
def delete_update(uid):
    updates = load_data()
    updates = [u for u in updates if u["id"] != uid]
    save_data(updates)
    return jsonify({"ok": True})


@app.post("/api/archive/<uid>")
def archive_update(uid):
    updates = load_data()
    update = find_update(updates, uid)
    if update:
        update["status"] = "archived"
        update["archived_at"] = datetime.now().isoformat()
        save_data(updates)
    return jsonify({"ok": True})


# ── Import API ────────────────────────────────────────────────────────────────

@app.post("/api/import/audio")
def import_audio():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    meeting_name = request.form.get("meeting_name", "").strip()
    language = request.form.get("language", "pt").strip()

    suffix = Path(file.filename).suffix or ".wav"
    tmp = Path(tempfile.mktemp(suffix=suffix))
    file.save(tmp)

    job_id = _start_job(_run_audio_job, tmp, meeting_name, language)
    return jsonify({"job_id": job_id})


@app.post("/api/import/transcript")
def import_transcript():
    transcript_text = request.form.get("content", "").strip()
    meeting_name = request.form.get("meeting_name", "").strip()

    if not transcript_text:
        # Try file upload
        if "file" in request.files:
            transcript_text = request.files["file"].read().decode("utf-8").strip()
    if not transcript_text:
        return jsonify({"error": "No transcript content provided"}), 400

    job_id = _start_job(_run_transcript_job, transcript_text, meeting_name)
    return jsonify({"job_id": job_id})


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = _jobs.get(job_id, {"status": "not_found"})
    return jsonify(job)


@app.post("/api/reprocess/<uid>")
def reprocess_update(uid):
    data = request.get_json(force=True)
    transcript_text = data.get("transcript", "").strip()
    hint = data.get("hint", "").strip()
    if not transcript_text:
        return jsonify({"error": "No transcript provided"}), 400
    updates = load_data()
    if not find_update(updates, uid):
        return jsonify({"error": "Not found"}), 404
    job_id = _start_job(_run_reprocess_job, uid, transcript_text, hint)
    return jsonify({"job_id": job_id})


@app.post("/api/explore")
def explore():
    data = request.get_json(force=True)
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    job_id = _start_job(_run_explore_job, query)
    return jsonify({"job_id": job_id})


@app.post("/api/generate-combined")
def generate_combined():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    name = data.get("name", "").strip()

    if len(ids) < 2:
        return jsonify({"error": "Select at least 2 updates"}), 400

    updates = load_data()
    selected = [u for u in updates if u["id"] in ids]

    if not selected:
        return jsonify({"error": "No updates found"}), 404

    job_id = _start_job(_run_combined_job, selected, name)
    return jsonify({"job_id": job_id})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Starting web platform at http://localhost:{PORT}")
    app.run(port=PORT, debug=False, threaded=True)
