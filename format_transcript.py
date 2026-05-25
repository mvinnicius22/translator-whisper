#!/usr/bin/env python3
"""
Post-processes a raw Whisper transcript:
  1. Strips timestamps and deduplicates Whisper repetition artifacts
  2. Joins 2-second fragments into sentence-level lines
  3. Splits into chunks and sends each to Claude for prose formatting
  4. Saves the final polished transcript alongside the original
"""

import re
import subprocess
import sys
from pathlib import Path

CHUNK_MINUTES = 20          # audio minutes per Claude call
MIN_PARA_CHARS = 120        # minimum chars before breaking a paragraph

AGENT_PROMPT = """\
Você é um editor especializado em pós-processamento de transcrições em português brasileiro.

Você receberá um trecho de transcrição já pré-processado (timestamps removidos, fragmentos unidos). \
Sua tarefa é devolver esse trecho formatado de forma limpa e fluida, SEM PERDER NENHUM DETALHE \
e SEM RESUMIR nada.

Regras:
1. Organize em parágrafos coerentes. Não resuma nem corte conteúdo.
2. Use travessão (—) para marcar troca de voz/interrupção em trechos dialógicos. \
   Em monólogos contínuos, apenas paragrafe normalmente.
3. Corrija nomes próprios que o Whisper claramente errou (use o contexto para inferir). \
   Exemplos comuns: nomes de tecnologias, frameworks, marcas e pessoas mencionadas no áudio.
4. Corrija pontuação e acentuação onde for erro claro da transcrição.
5. PRESERVE o tom coloquial e informal do áudio original.
6. Não adicione introduções, conclusões ou comentários — retorne apenas o trecho formatado.
"""


# ── Step 1: mechanical pre-processing ────────────────────────────────────────

TS_RE = re.compile(r'^\*\*\[\d[\d:]*\]\*\*\s*')


def preprocess(raw: str) -> list[str]:
    """
    Returns a list of joined, deduplicated sentence-level strings
    (no timestamps, no blank lines).
    """
    lines = []
    prev = None
    for line in raw.splitlines():
        line = TS_RE.sub('', line).strip()
        if not line:
            continue
        if line == prev:          # Whisper repetition artifact
            continue
        lines.append(line)
        prev = line

    # Join fragments into sentence-level chunks heuristically:
    # flush when the accumulated text ends with sentence-ending punctuation
    # or when a new apparent sentence starts with a capital after lowercase.
    sentences: list[str] = []
    buf = ''
    for line in lines:
        if buf:
            # Check if previous ended a sentence
            ends_sentence = buf.rstrip()[-1] in '.!?'
            # Check if this line starts a new sentence (capital after sentence end)
            starts_new = ends_sentence and line and line[0].isupper()
            if starts_new:
                sentences.append(buf.strip())
                buf = line
            else:
                buf = buf.rstrip() + ' ' + line
        else:
            buf = line
    if buf.strip():
        sentences.append(buf.strip())

    return sentences


# ── Step 2: split into timed chunks ──────────────────────────────────────────

def split_chunks(raw_lines: list[str], raw_full: str) -> list[list[str]]:
    """
    Re-reads the original timestamped lines to find chunk boundaries,
    then maps those onto the pre-processed sentences.
    Chunk boundary = every CHUNK_MINUTES of audio.
    """
    # Build a map: sentence index → approx audio second
    # We pair each raw line with its timestamp, then map to sentences.
    ts_seconds: list[float] = []
    texts_raw: list[str] = []

    for line in raw_full.splitlines():
        m = re.match(r'^\*\*\[(\d+):(\d+)(?::(\d+))?\]\*\*\s*(.*)', line)
        if not m:
            continue
        if m.group(3) is not None:  # HH:MM:SS
            s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        else:                        # MM:SS
            s = int(m.group(1)) * 60 + int(m.group(2))
        texts_raw.append(m.group(4).strip())
        ts_seconds.append(float(s))

    if not ts_seconds:
        return [raw_lines]

    total = ts_seconds[-1]
    chunk_sec = CHUNK_MINUTES * 60
    n_chunks = max(1, int(total // chunk_sec) + 1)
    boundaries = [i * chunk_sec for i in range(n_chunks + 1)]

    # Find which raw-line index belongs to each chunk
    raw_chunk_indices: list[list[int]] = [[] for _ in range(n_chunks)]
    for i, sec in enumerate(ts_seconds):
        chunk_idx = min(int(sec // chunk_sec), n_chunks - 1)
        raw_chunk_indices[chunk_idx].append(i)

    # Map pre-processed sentences to chunks by matching text content
    # (simple heuristic: divide sentences proportionally by raw-line counts)
    total_raw = len(texts_raw)
    total_sent = len(raw_lines)
    chunks: list[list[str]] = []
    sent_cursor = 0
    for chunk_i, indices in enumerate(raw_chunk_indices):
        if not indices:
            continue
        # Fraction of raw lines → fraction of sentences
        frac = len(indices) / total_raw if total_raw else 0
        n_sent = max(1, round(frac * total_sent))
        if chunk_i == n_chunks - 1:
            n_sent = total_sent - sent_cursor     # last chunk gets remainder
        chunk_sents = raw_lines[sent_cursor: sent_cursor + n_sent]
        if chunk_sents:
            chunks.append(chunk_sents)
        sent_cursor += n_sent

    return chunks if chunks else [raw_lines]


# ── Step 3: call Claude on each chunk ────────────────────────────────────────

def call_claude(text: str) -> str:
    prompt = f"{AGENT_PROMPT}\n\nTrecho da transcrição:\n\n{text}"
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [WARN] Claude returned non-zero exit: {result.stderr[:200]}",
              file=sys.stderr)
        return text     # fall back to pre-processed text
    return result.stdout.strip()


# ── Step 4: build final markdown ─────────────────────────────────────────────

def _build_header(raw: str, src: Path) -> str:
    title_match = re.search(r'^# (.+)', raw, re.MULTILINE)
    title = title_match.group(1) if title_match else f"Transcrição — {src.name}"
    info_match = re.search(r'(## Info(?:rmações)?\n.*?)(?=\n## |\Z)', raw, re.DOTALL)
    info_block = info_match.group(1).rstrip() if info_match else ""
    return f"# {title}\n\n{info_block}\n\n## Transcrição\n\n"


def _build_footer() -> str:
    from datetime import date
    today = date.today().strftime('%d/%m/%Y')
    return f"\n\n---\n*Transcrição automática pós-editada em {today}*\n"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: format_transcript.py <transcript.md> [output.md]")
        sys.exit(1)

    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.parent / "transcript_formatted.md"

    raw = src.read_text(encoding="utf-8")

    # Extract only the ## Transcript / ## Transcrição section
    match = re.search(r'## Transcript\n(.*)', raw, re.DOTALL)
    if not match:
        match = re.search(r'## Transcrição\n(.*)', raw, re.DOTALL)
    transcript_raw = match.group(1).strip() if match else raw

    print("Pre-processing transcript...")
    sentences = preprocess(transcript_raw)
    print(f"  {len(sentences)} sentence-level chunks after dedup+join")

    print("Splitting into timed segments...")
    chunks = split_chunks(sentences, transcript_raw)
    print(f"  {len(chunks)} chunks of ~{CHUNK_MINUTES} min each")

    formatted_parts: list[str] = []
    for i, chunk in enumerate(chunks):
        start_min = i * CHUNK_MINUTES
        end_min = (i + 1) * CHUNK_MINUTES
        print(f"  Calling Claude on chunk {i+1}/{len(chunks)} "
              f"({start_min}–{end_min} min, {len(chunk)} sentences)...")
        text = '\n'.join(chunk)
        formatted = call_claude(text)
        formatted_parts.append(formatted)

    body = '\n\n'.join(formatted_parts)
    out.write_text(_build_header(raw, src) + body + _build_footer(), encoding="utf-8")
    print(f"\nDone. Saved to:\n  {out}")


if __name__ == "__main__":
    main()
