#!/usr/bin/env python3
"""
File transcriber — transcribes a video/audio file using OpenAI Whisper.
Accepts any format ffmpeg supports (mp4, mov, mkv, mp3, wav, etc.).
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

# Matches Whisper's verbose segment header: [00:00.000 --> 01:23.456]
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
    """Parse a Whisper timestamp like '01:23.456' or '1:23:45.678' to seconds."""
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return int(parts[0]) * 60 + float(parts[1])


def _get_duration(file_path: str) -> float:
    """Return audio duration in seconds via ffprobe, or 0 on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", file_path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


class _ProgressWriter:
    """
    Wraps sys.stdout during Whisper transcription.
    Passes all text through tqdm.write() (so it appears above the progress bar),
    and advances the tqdm bar whenever a segment timestamp is detected.
    """

    def __init__(self, bar: tqdm):
        self._bar = bar
        self._real = sys.__stdout__
        self._pos = 0.0
        self._buf = ""

    def write(self, text: str) -> None:
        self._buf += text
        # Flush complete lines through tqdm.write so they stay above the bar
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            tqdm.write(line, file=self._real)
            m = _SEGMENT_RE.search(line)
            if m:
                pos = _parse_ts(m.group(1))
                delta = max(0, int(pos - self._pos))
                if delta:
                    self._bar.update(delta)
                    self._pos = pos

    def flush(self) -> None:
        # Flush any remaining partial line
        if self._buf:
            tqdm.write(self._buf, file=self._real, end="")
            self._buf = ""
        self._real.flush()


_MIN_PARA_CHARS = 120  # accumulate sentences until this length before breaking a paragraph


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
        elif len(buf) >= _MIN_PARA_CHARS:
            paragraphs.append(buf)
            buf = sentence
        else:
            buf += " " + sentence
    if buf:
        paragraphs.append(buf)

    return "\n\n".join(paragraphs)


def _build_markdown(file_path: str, model_size: str, language: str,
                    segments: list, session_start: datetime, elapsed: float,
                    fmt: str = "timestamped") -> str:
    name = Path(file_path).name
    total_secs = int(segments[-1]["end"]) if segments else 0
    audio_dur = f"{total_secs // 3600}h {(total_secs % 3600) // 60}m {total_secs % 60}s"

    lines = [
        f"# Transcript — {name}",
        "",
        "## Info",
        "",
        "| | |",
        "|---|---|",
        f"| **File** | {name} |",
        f"| **Date** | {session_start.strftime('%d/%m/%Y')} |",
        f"| **Language** | {language} |",
        f"| **Model** | {model_size} |",
        f"| **Audio duration** | {audio_dur} |",
        f"| **Processing time** | {elapsed / 60:.1f} min |",
        f"| **Format** | {fmt} |",
        "",
        "## Transcript",
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
        f"*Auto-generated on {session_start.strftime('%d/%m/%Y')} at "
        f"{session_start.strftime('%H:%M')} — model: {model_size}*",
    ]
    return "\n".join(lines)


def transcribe_file(file_path: str, model_size: str, language: str,
                    meeting_name: str, fmt: str = "timestamped") -> Path:
    device = _device()
    print(f"Device   : {device}")
    print(f"Model    : {model_size}")
    print(f"Language : {language}")
    print(f"File     : {file_path}")
    print()

    print(f"Loading Whisper '{model_size}' model (downloading if first use)...")
    model = whisper.load_model(model_size, device=device)
    print("Model loaded.\n")

    total_secs = _get_duration(file_path)
    total_int = int(total_secs) if total_secs > 0 else 1

    bar = tqdm(
        total=total_int,
        unit="s",
        ncols=80,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}s  [{elapsed}<{remaining}]",
        file=sys.stderr,
    )

    print("Transcribing — each segment appears as it's processed:\n")
    t0 = time.time()

    sys.stdout = _ProgressWriter(bar)
    try:
        result = model.transcribe(
            file_path,
            language=language,
            task="transcribe",
            verbose=True,
            condition_on_previous_text=True,
            no_speech_threshold=0.6,
            fp16=(device == "cuda"),
        )
    finally:
        sys.stdout = sys.__stdout__
        bar.update(bar.total - bar.n)   # fill to 100%
        bar.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed / 60:.1f} minutes.\n")

    session_start = datetime.now()
    label = meeting_name or Path(file_path).stem
    folder = get_meeting_folder(label, session_start)
    folder.mkdir(parents=True, exist_ok=True)

    transcript_path = folder / "transcript.md"
    transcript_path.write_text(
        _build_markdown(file_path, model_size, language,
                        result.get("segments", []), session_start, elapsed, fmt),
        encoding="utf-8",
    )

    print(f"Transcript saved at:\n  {transcript_path}")
    print("\nTo run a post-processing agent (summary, actions, etc.), run:")
    print("  ./process.sh")
    return transcript_path


def main():
    parser = argparse.ArgumentParser(description="Transcribe a video/audio file with Whisper")
    parser.add_argument("--file", required=True,
                        help="Path to the video or audio file (mp4, mov, mp3, wav, ...)")
    parser.add_argument("--model", default="medium",
                        choices=["tiny", "base", "small", "medium",
                                 "large", "large-v2", "large-v3", "turbo"],
                        help="Whisper model size (default: medium)")
    parser.add_argument("--lang", default="pt",
                        help="Audio language code, e.g. pt, en, es (default: pt)")
    parser.add_argument("--meeting-name", default="",
                        help="Name for the output folder (defaults to filename)")
    parser.add_argument("--format", default="timestamped",
                        choices=["timestamped", "prose"],
                        help="timestamped: each segment prefixed with [MM:SS] (default); "
                             "prose: clean continuous text without timestamps")
    args = parser.parse_args()

    if not Path(args.file).exists():
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    transcribe_file(args.file, args.model, args.lang, args.meeting_name, args.format)


if __name__ == "__main__":
    main()
