#!/usr/bin/env python3
"""
Lecture notes orchestrator.
Stage A: assemble agent input (concat transcripts + aux material).
Stage B: call Claude CLI → lecture_notes.md.
Stage C: render lecture_notes.md → lecture_notes.pdf + flashcards.csv.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


# ── Stage A helpers ───────────────────────────────────────────────────────────

def _extract_body(transcript_md: str) -> str:
    """Strip the metadata table, keep only the transcript body text."""
    # Find the ## Transcript / ## Transcrição heading and take everything after
    match = re.search(
        r'^##\s+(Transcript|Transcri[çc][aã]o)\s*$',
        transcript_md,
        re.MULTILINE | re.IGNORECASE,
    )
    if match:
        return transcript_md[match.end():].strip()
    # Fallback: return everything after the first --- separator block
    parts = re.split(r'^---\s*$', transcript_md, flags=re.MULTILINE)
    return parts[-1].strip() if len(parts) > 1 else transcript_md.strip()


def _last_timestamp_seconds(body: str) -> float:
    """Return the last [HH:MM:SS] or [MM:SS] timestamp found in body, as seconds."""
    matches = re.findall(r'\*?\*?\[(\d{1,2}:\d{2}(?::\d{2})?)\]\*?\*?', body)
    if not matches:
        return 0.0
    last = matches[-1]
    parts = last.split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return int(parts[0]) * 60 + float(parts[1])


def _offset_timestamps(body: str, offset_secs: float) -> str:
    """Rewrite [MM:SS] / [HH:MM:SS] timestamps by adding offset_secs."""
    if offset_secs == 0:
        return body

    def _replace(m):
        ts = m.group(1)
        parts = ts.split(':')
        if len(parts) == 3:
            total = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        else:
            total = int(parts[0]) * 60 + float(parts[1])
        total += offset_secs
        h = int(total // 3600)
        rem = total % 3600
        mi = int(rem // 60)
        s = int(rem % 60)
        if h:
            new_ts = f"{h:02d}:{mi:02d}:{s:02d}"
        else:
            new_ts = f"{mi:02d}:{s:02d}"
        return m.group(0).replace(ts, new_ts)

    return re.sub(r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]', _replace, body)


def assemble_transcript(transcript_paths: list[Path]) -> str:
    """Concatenate transcript bodies with clip markers; offset timestamps if present."""
    segments = []
    cumulative = 0.0

    for i, path in enumerate(transcript_paths, 1):
        raw = path.read_text(encoding='utf-8')
        body = _extract_body(raw)

        # Detect timestamped vs prose (timestamped has [MM:SS] patterns)
        has_timestamps = bool(re.search(r'\[\d{1,2}:\d{2}\]', body))

        # Capture duration BEFORE offsetting — the offset body's last timestamp
        # would be (original_last + cumulative), doubling the error each clip.
        clip_duration = _last_timestamp_seconds(body) if has_timestamps else 0.0

        if has_timestamps and cumulative > 0:
            body = _offset_timestamps(body, cumulative)

        segments.append(f"<!-- clip {i} -->\n\n{body}")

        cumulative += clip_duration

    return "\n\n".join(segments)


def extract_aux_material(aux_path: Path, slides_dir: Path) -> str:
    """
    Extract text and page images from a PDF.
    Returns text block for injection into agent input.
    Requires PyMuPDF (fitz). OCR via pytesseract is used when a page has no text layer.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("Warning: PyMuPDF not installed — skipping aux material.", file=sys.stderr)
        return ""

    slides_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(aux_path))
    pages_text = []

    for i, page in enumerate(doc, 1):
        # Render page image
        mat = fitz.Matrix(2.0, 2.0)  # 2x scale for readability
        pix = page.get_pixmap(matrix=mat)
        img_path = slides_dir / f"slide-{i}.png"
        pix.save(str(img_path))

        # Extract text
        text = page.get_text().strip()
        if not text:
            # Try OCR fallback
            try:
                import pytesseract
                from PIL import Image
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text = pytesseract.image_to_string(img, lang='por').strip()
            except ImportError:
                text = ""

        if text:
            pages_text.append(f"[Slide {i}]\n{text}")

    doc.close()

    if not pages_text:
        return ""

    combined = "\n\n".join(pages_text)
    return f"<material_de_apoio>\n{combined}\n</material_de_apoio>"


# ── Stage B: run agent ────────────────────────────────────────────────────────

def run_agent(agent_path: Path, payload: str) -> str:
    agent_prompt = agent_path.read_text(encoding='utf-8')
    full_prompt = f"{agent_prompt}\n\nHere is the lecture transcript:\n\n{payload}"

    result = subprocess.run(
        ["claude", "-p", full_prompt],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error running Claude CLI:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    return result.stdout


# ── Stage C: render ───────────────────────────────────────────────────────────

def render(notes_md: Path, slides_dir: Path | None):
    render_script = Path(__file__).parent / "render_notes.py"
    cmd = [
        sys.executable, str(render_script),
        "--md", str(notes_md),
    ]
    if slides_dir and slides_dir.exists():
        cmd += ["--slides-dir", str(slides_dir)]
    subprocess.run(cmd, check=True)


# ── Code-fence stripping ──────────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    """Remove code-fence wrappers that Claude CLI adds around agent output.

    Handles two patterns:
    1. Entire output wrapped:  ```markdown\\n...\\n```
    2. Body-only wrapped (YAML comes before the fence):
         ---\\nyaml\\n---\\n```markdown\\nbody\\n```
    """
    s = text.strip()

    def _peel(s: str) -> str:
        """Remove one opening+closing code fence pair."""
        if not s.startswith("```"):
            return s
        nl = s.index('\n')
        inner = s[nl + 1:]
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3].rstrip()
        return inner

    # Case 1: whole output is fenced
    if s.startswith("```"):
        return _peel(s)

    # Case 2: YAML front matter, then a fenced body
    if s.startswith("---"):
        # Find the closing --- of the front matter
        m = re.search(r'(?m)^---\s*$', s[3:])
        if m:
            yaml_end = 3 + m.end()          # index right after closing ---\n
            body = s[yaml_end:].lstrip('\n')
            if body.startswith("```"):
                body = _peel(body)
            return s[:yaml_end] + '\n' + body

    return s


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build lecture notes PDF from transcripts")
    parser.add_argument(
        "--transcripts", nargs="+", required=True,
        help="Ordered list of transcript.md paths"
    )
    parser.add_argument("--aux", default="", help="Path to auxiliary PDF")
    parser.add_argument("--output-dir", default="",
                        help="Directory to save lecture_notes.md/pdf (default: first transcript's folder)")
    parser.add_argument("--skip-agent", action="store_true",
                        help="Re-render existing lecture_notes.md without calling the agent")
    parser.add_argument("--md-only", action="store_true",
                        help="Stop after producing lecture_notes.md (skip PDF render)")
    args = parser.parse_args()

    transcript_paths = [Path(p) for p in args.transcripts]
    for p in transcript_paths:
        if not p.exists():
            print(f"Error: transcript not found: {p}", file=sys.stderr)
            sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else transcript_paths[0].parent
    notes_md = output_dir / "lecture_notes.md"
    slides_dir = output_dir / "slides" if args.aux else None

    dir_root = Path(__file__).parent
    agent_path = dir_root / "agents" / "lecture_notes.md"

    if not args.skip_agent:
        print("Stage A: assembling agent input...")
        transcript_text = assemble_transcript(transcript_paths)

        aux_block = ""
        if args.aux:
            aux_path = Path(args.aux)
            if aux_path.exists():
                print(f"  Extracting aux material: {aux_path.name}")
                aux_block = extract_aux_material(aux_path, slides_dir)
            else:
                print(f"Warning: aux file not found: {args.aux}", file=sys.stderr)

        payload = aux_block + "\n\n" + transcript_text if aux_block else transcript_text

        print("Stage B: running lecture_notes agent (this may take a few minutes)...")
        output = run_agent(agent_path, payload)

        output = _strip_code_fences(output)

        notes_md.write_text(output, encoding='utf-8')
        print(f"  Saved: {notes_md}")

    if not notes_md.exists():
        print(f"Error: {notes_md} not found. Run without --skip-agent first.", file=sys.stderr)
        sys.exit(1)

    if args.md_only:
        print(f"\nDone. Review and edit {notes_md}, then re-run with --skip-agent to render PDF.")
        return

    print("Stage C: rendering PDF...")
    render(notes_md, slides_dir)


if __name__ == "__main__":
    main()
