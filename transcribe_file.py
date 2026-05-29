#!/usr/bin/env python3
"""
File transcriber — transcribes a video/audio file using OpenAI Whisper.
Accepts any format ffmpeg supports (mp4, mov, mkv, mp3, wav, etc.).
"""

import argparse
import json
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


def _patch_dtw_for_mps() -> None:
    """Whisper's word-timestamp DTW casts to float64 (unsupported on MPS).
    Move the alignment tensor to CPU first; the rest of inference stays on MPS."""
    import whisper.timing as _timing
    if getattr(_timing.dtw, "_mps_patched", False):
        return
    _orig = _timing.dtw

    def _dtw(x):
        return _orig(x.cpu())

    _dtw._mps_patched = True
    _timing.dtw = _dtw


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


def _format_speaker(raw_label: str) -> str:
    """Convert pyannote labels (SPEAKER_00, SPEAKER_01, ...) to readable names."""
    m = re.match(r'SPEAKER_(\d+)', raw_label)
    if m:
        return f"Speaker {int(m.group(1)) + 1}"
    return raw_label


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


def _prose_text_with_speakers(segments: list) -> str:
    """Group consecutive same-speaker segments into labeled prose blocks."""
    blocks: list[tuple[str, list[str]]] = []
    current_speaker: str | None = None
    current_parts: list[str] = []

    for seg in segments:
        text = seg["text"].strip()
        if text.lower().strip(".") in HALLUCINATIONS:
            continue
        speaker = _format_speaker(seg.get("speaker", "SPEAKER_00"))
        if speaker != current_speaker:
            if current_parts:
                blocks.append((current_speaker, current_parts))  # type: ignore[arg-type]
            current_speaker = speaker
            current_parts = [text]
        else:
            current_parts.append(text)

    if current_parts:
        blocks.append((current_speaker, current_parts))  # type: ignore[arg-type]

    return "\n\n".join(f"**{spk}:**\n\n{' '.join(parts)}" for spk, parts in blocks)


def _build_markdown(file_path: str, model_size: str, language: str,
                    segments: list, session_start: datetime, elapsed: float,
                    fmt: str = "timestamped") -> str:
    name = Path(file_path).name
    total_secs = int(segments[-1]["end"]) if segments else 0
    audio_dur = f"{total_secs // 3600}h {(total_secs % 3600) // 60}m {total_secs % 60}s"
    has_speakers = bool(segments) and "speaker" in segments[0]

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
    ]

    if has_speakers:
        speaker_names = list(dict.fromkeys(_format_speaker(s["speaker"]) for s in segments))
        lines.append(f"| **Speakers** | {', '.join(speaker_names)} |")

    lines += ["", "## Transcript", ""]

    if fmt == "prose":
        if has_speakers:
            lines.append(_prose_text_with_speakers(segments))
        else:
            lines.append(_prose_text(segments))
    else:
        for seg in segments:
            text = seg["text"].strip()
            if text.lower().strip(".") in HALLUCINATIONS:
                continue
            if has_speakers:
                speaker = _format_speaker(seg.get("speaker", "SPEAKER_00"))
                lines.append(f"**[{_format_ts(seg['start'])}] {speaker}:** {text}")
            else:
                lines.append(f"**[{_format_ts(seg['start'])}]** {text}")

    lines += [
        "",
        "---",
        f"*Auto-generated on {session_start.strftime('%d/%m/%Y')} at "
        f"{session_start.strftime('%H:%M')} — model: {model_size}*",
    ]
    return "\n".join(lines)


def transcribe_file(file_path: str, model_size: str, language: str,
                    meeting_name: str, fmt: str = "timestamped",
                    diarize: bool = False,
                    num_speakers: int | None = None,
                    speaker_turns: list | None = None,
                    output_folder: Path | None = None,
                    session_start: datetime | None = None) -> Path:
    device = _device()
    print(f"Device   : {device}")
    print(f"Model    : {model_size}")
    print(f"Language : {language}")
    print(f"File     : {file_path}")
    print()

    print(f"Loading Whisper '{model_size}' model (downloading if first use)...")
    model = whisper.load_model(model_size, device=device)
    print("Model loaded.\n")

    if diarize and device == "mps":
        _patch_dtw_for_mps()

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
            word_timestamps=diarize,
        )
    finally:
        sys.stdout = sys.__stdout__
        bar.update(bar.total - bar.n)   # fill to 100%
        bar.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed / 60:.1f} minutes.\n")

    segments = result.get("segments", [])

    if diarize:
        from diarize import extract_wav, diarize as run_diarize, assign_speakers
        if speaker_turns is not None:
            segments = assign_speakers(segments, speaker_turns)
        else:
            print("Running speaker diarization (this may take several minutes)...")
            wav = extract_wav(file_path)
            try:
                turns = run_diarize(wav, num_speakers=num_speakers)
                segments = assign_speakers(segments, turns)
            finally:
                wav.unlink(missing_ok=True)
        n_speakers = len(set(s["speaker"] for s in segments))
        print(f"Diarization complete. Detected {n_speakers} speaker(s).\n")

    if session_start is None:
        session_start = datetime.now()
    label = meeting_name or Path(file_path).stem
    folder = output_folder or get_meeting_folder(label, session_start)
    folder.mkdir(parents=True, exist_ok=True)

    transcript_path = folder / "transcript.md"
    transcript_path.write_text(
        _build_markdown(file_path, model_size, language,
                        segments, session_start, elapsed, fmt),
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
    parser.add_argument("--model", default="large-v3",
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
    parser.add_argument("--diarize", action="store_true",
                        help="Detect speakers (requires diarization add-on and HuggingFace token)")
    parser.add_argument("--num-speakers", type=int, default=None,
                        help="Exact number of speakers, if known (improves accuracy; "
                             "default: auto-detect)")
    parser.add_argument("--output-folder", default="",
                        help="Save transcript to this folder instead of ~/Documents/Meetings/...")
    args = parser.parse_args()

    if not Path(args.file).exists():
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    if args.diarize:
        settings_path = Path(__file__).parent / "settings.json"
        if settings_path.exists():
            d = json.load(open(settings_path))
            if "diarize" not in d.get("installed_modes", []):
                print("Speaker diarization add-on is not installed.", file=sys.stderr)
                print("Run ./setup.sh and choose 'Add speaker diarization'.", file=sys.stderr)
                sys.exit(1)

    output_folder = Path(args.output_folder) if args.output_folder else None
    transcribe_file(args.file, args.model, args.lang, args.meeting_name,
                    args.format, args.diarize, args.num_speakers,
                    output_folder=output_folder)


if __name__ == "__main__":
    main()
