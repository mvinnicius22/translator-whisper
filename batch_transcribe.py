#!/usr/bin/env python3
"""
Batch transcription — loads Whisper once, transcribes all files in a directory.
Usage: python batch_transcribe.py --dir <path> --model large-v3 --lang pt --format prose --prefix <name>
"""

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import torch
import whisper
from tqdm import tqdm

from folders import get_meeting_folder

HALLUCINATIONS = {
    "", ".", "..", "...", "uh", "um",
    "thank you", "thanks for watching", "please subscribe",
    "thanks", "bye", "okay",
}

_SEGMENT_RE = re.compile(r'\[[\d:\.]+\s*-->\s*([\d:\.]+)\]')


def _device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _format_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_ts(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return int(parts[0]) * 60 + float(parts[1])


def _get_duration(file_path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", file_path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def _prose_text(segments: list) -> str:
    parts = []
    for seg in segments:
        text = seg["text"].strip()
        if text.lower().strip(".") in HALLUCINATIONS:
            continue
        parts.append(text)
    joined = " ".join(parts)
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', joined)
    paragraphs = []
    buf = ""
    for sentence in sentences:
        if not buf:
            buf = sentence
        elif len(buf) >= 120:
            paragraphs.append(buf)
            buf = sentence
        else:
            buf += " " + sentence
    if buf:
        paragraphs.append(buf)
    return "\n\n".join(paragraphs)


def _build_markdown(file_path: str, model_size: str, language: str,
                    segments: list, session_start: datetime, elapsed: float,
                    fmt: str = "prose") -> str:
    name = Path(file_path).name
    total_secs = int(segments[-1]["end"]) if segments else 0
    audio_dur = f"{total_secs // 3600}h {(total_secs % 3600) // 60}m {total_secs % 60}s"

    lines = [
        f"# Transcrição — {name}",
        "",
        "## Info",
        "",
        "| | |",
        "|---|---|",
        f"| **Arquivo** | {name} |",
        f"| **Data** | {session_start.strftime('%d/%m/%Y')} |",
        f"| **Idioma** | {language} |",
        f"| **Modelo** | {model_size} |",
        f"| **Duração do áudio** | {audio_dur} |",
        f"| **Tempo de processamento** | {elapsed / 60:.1f} min |",
        f"| **Formato** | {fmt} |",
        "",
        "## Transcrição",
        "",
    ]

    if fmt == "prose":
        lines.append(_prose_text(segments))
    else:
        for seg in segments:
            text = seg["text"].strip()
            if text.lower().strip(".") in HALLUCINATIONS:
                continue
            lines.append(f"**[{_format_ts(seg['start'])}]** {text}")

    lines += [
        "",
        "---",
        f"*Gerado automaticamente em {session_start.strftime('%d/%m/%Y')} às "
        f"{session_start.strftime('%H:%M')} — modelo: {model_size}*",
    ]
    return "\n".join(lines)


def transcribe_one(model, file_path: str, model_size: str, language: str,
                   meeting_name: str, fmt: str) -> Path:
    print(f"\n{'='*60}")
    print(f"  Arquivo: {Path(file_path).name}")
    print(f"{'='*60}")

    total_secs = _get_duration(file_path)
    total_int = int(total_secs) if total_secs > 0 else 1

    bar = tqdm(
        total=total_int,
        unit="s",
        ncols=80,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}s  [{elapsed}<{remaining}]",
        file=sys.stderr,
    )

    t0 = time.time()
    orig_stdout = sys.stdout

    class _Writer:
        def __init__(self):
            self._pos = 0.0
            self._buf = ""

        def write(self, text):
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                tqdm.write(line, file=orig_stdout)
                m = _SEGMENT_RE.search(line)
                if m:
                    pos = _parse_ts(m.group(1))
                    delta = max(0, int(pos - self._pos))
                    if delta:
                        bar.update(delta)
                        self._pos = pos

        def flush(self):
            if self._buf:
                tqdm.write(self._buf, file=orig_stdout, end="")
                self._buf = ""
            orig_stdout.flush()

    sys.stdout = _Writer()
    try:
        result = model.transcribe(
            file_path,
            language=language,
            task="transcribe",
            verbose=True,
            condition_on_previous_text=True,
            no_speech_threshold=0.6,
            fp16=(_device() == "cuda"),
        )
    finally:
        sys.stdout = orig_stdout
        bar.update(bar.total - bar.n)
        bar.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed / 60:.1f} minutes.")

    session_start = datetime.now()
    folder = get_meeting_folder(meeting_name, session_start)
    folder.mkdir(parents=True, exist_ok=True)

    transcript_path = folder / "transcript.md"
    transcript_path.write_text(
        _build_markdown(file_path, model_size, language,
                        result.get("segments", []), session_start, elapsed, fmt),
        encoding="utf-8",
    )
    print(f"Salvo em: {transcript_path}")
    return transcript_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--model", default="large-v3",
                        choices=["tiny", "base", "small", "medium",
                                 "large", "large-v2", "large-v3", "turbo"])
    parser.add_argument("--lang", default="pt")
    parser.add_argument("--format", default="prose", choices=["timestamped", "prose"])
    parser.add_argument("--prefix", default="plano-de-tratamento")
    args = parser.parse_args()

    audio_dir = Path(args.dir)
    extensions = {".m4a", ".mp3", ".wav", ".mp4", ".mov", ".mkv", ".ogg", ".flac"}
    files = sorted(f for f in audio_dir.iterdir() if f.suffix.lower() in extensions)

    if not files:
        print("No audio files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} audio file(s):")
    for i, f in enumerate(files, 1):
        print(f"  {i}. {f.name}")

    device = _device()
    print(f"\nDevice : {device}")
    print(f"Model  : {args.model}")
    print(f"Loading Whisper '{args.model}' (downloading if first use)...")
    model = whisper.load_model(args.model, device=device)
    print("Model loaded.\n")

    saved = []
    for i, f in enumerate(files, 1):
        name = f"{args.prefix}-clipe-{i}"
        path = transcribe_one(model, str(f), args.model, args.lang, name, args.format)
        saved.append(path)

    print(f"\n{'='*60}")
    print(f"All {len(saved)} transcript(s) saved:")
    for p in saved:
        print(f"  {p}")


if __name__ == "__main__":
    main()
