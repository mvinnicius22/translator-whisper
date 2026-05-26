#!/usr/bin/env python3
"""
Speaker diarization using pyannote.audio.
Shared module used by file transcription (--diarize) and real-time post-mode.
"""

import os
import subprocess
import tempfile
from pathlib import Path


def extract_wav(file_path: str | Path) -> Path:
    """Extract 16 kHz mono WAV from any ffmpeg-supported format to a temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(file_path),
         "-ar", "16000", "-ac", "1", "-f", "wav", tmp.name],
        capture_output=True, check=True,
    )
    return Path(tmp.name)


def read_token() -> str:
    """Read HuggingFace token from HF_TOKEN env var or .hf_token file."""
    env_token = os.environ.get("HF_TOKEN", "").strip()
    if env_token:
        return env_token
    token_path = Path(__file__).parent / ".hf_token"
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "HuggingFace token not found.\n"
        "Run ./setup.sh and choose 'Add speaker diarization', "
        "or set the HF_TOKEN environment variable."
    )


def diarize(wav_path: str | Path, hf_token: str | None = None,
            num_speakers: int | None = None) -> list[tuple[float, float, str]]:
    """
    Run pyannote speaker diarization on a WAV file.
    Returns a list of (start_sec, end_sec, speaker_label) tuples.
    When num_speakers is set, pyannote is told the exact speaker count
    instead of inferring it (avoids merging/splitting similar voices).
    """
    import torch
    from pyannote.audio import Pipeline

    if hf_token is None:
        hf_token = read_token()

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )

    if torch.backends.mps.is_available():
        try:
            pipeline.to(torch.device("mps"))
        except Exception:
            pass  # pyannote MPS support is incomplete; CPU fallback is fine

    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    result = pipeline(str(wav_path), **kwargs)
    annotation = result.speaker_diarization if hasattr(result, "speaker_diarization") else result
    return [
        (turn.start, turn.end, speaker)
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]


def mic_dominant_blocks(mic, sys_audio, sample_rate: int,
                        window: float = 0.5, threshold: float = 0.008,
                        min_duration: float = 0.4) -> list[tuple[float, float]]:
    """
    Find time ranges where the local microphone is the dominant source.
    Compares mic vs system energy per window; a window belongs to the local
    speaker when the mic is above the silence threshold and louder than system
    (so system bleed picked up by the mic does not count). Returns merged
    (start_sec, end_sec) blocks, dropping ones shorter than min_duration.
    """
    import numpy as np

    n = min(mic.size, sys_audio.size)
    w = max(1, int(sample_rate * window))
    blocks: list[tuple[float, float]] = []
    cur_start = cur_end = None
    for i in range(0, n, w):
        m = mic[i:i + w]
        s = sys_audio[i:i + w]
        m_rms = float(np.sqrt(np.mean(m ** 2))) if m.size else 0.0
        s_rms = float(np.sqrt(np.mean(s ** 2))) if s.size else 0.0
        if m_rms > threshold and m_rms > s_rms:
            if cur_start is None:
                cur_start = i / sample_rate
            cur_end = min(i + w, n) / sample_rate
        elif cur_start is not None:
            blocks.append((cur_start, cur_end))
            cur_start = cur_end = None
    if cur_start is not None:
        blocks.append((cur_start, cur_end))

    return [(s, e) for s, e in blocks if e - s >= min_duration]


def combine_turns(sys_turns: list[tuple[float, float, str]],
                  you_blocks: list[tuple[float, float]],
                  you_label: str = "You") -> list[tuple[float, float, str]]:
    """
    Merge system-channel diarization turns with local-mic 'you' blocks.
    The mic blocks take precedence: system turns are clipped to exclude any
    time the local speaker was dominant, then the 'you' blocks are added.
    """
    clipped: list[tuple[float, float, str]] = []
    for t_start, t_end, spk in sys_turns:
        pieces = [(t_start, t_end)]
        for b_start, b_end in you_blocks:
            next_pieces = []
            for s, e in pieces:
                if b_end <= s or b_start >= e:
                    next_pieces.append((s, e))
                else:
                    if s < b_start:
                        next_pieces.append((s, b_start))
                    if b_end < e:
                        next_pieces.append((b_end, e))
            pieces = next_pieces
        clipped += [(s, e, spk) for s, e in pieces if e - s > 0.05]

    clipped += [(s, e, you_label) for s, e in you_blocks]
    clipped.sort(key=lambda t: t[0])
    return clipped


def _speaker_for_interval(
    start: float, end: float,
    turns: list[tuple[float, float, str]],
) -> str:
    """Pick the speaker whose turn overlaps [start, end] most; if none overlaps,
    fall back to the nearest turn by gap to the interval midpoint."""
    best_speaker = None
    best_overlap = 0.0
    for t_start, t_end, speaker in turns:
        overlap = min(end, t_end) - max(start, t_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = speaker
    if best_speaker is not None:
        return best_speaker

    mid = (start + end) / 2
    best_dist = float("inf")
    for t_start, t_end, speaker in turns:
        dist = max(t_start - mid, mid - t_end, 0.0)
        if dist < best_dist:
            best_dist = dist
            best_speaker = speaker
    return best_speaker or "SPEAKER_00"


def assign_speakers(
    segments: list[dict],
    turns: list[tuple[float, float, str]],
) -> list[dict]:
    """
    Split Whisper segments at speaker boundaries using word-level timestamps,
    then regroup consecutive same-speaker words into single-speaker segments.

    Each input segment should carry a 'words' list (Whisper word_timestamps).
    Segments without word timing fall back to whole-segment overlap.
    Returns new segments, each with start, end, text and a 'speaker' key.
    """
    labeled: list[dict] = []
    for seg in segments:
        words = seg.get("words") or []
        if not words:
            spk = _speaker_for_interval(seg["start"], seg["end"], turns)
            labeled.append({**seg, "speaker": spk})
            continue

        run_speaker: str | None = None
        run_words: list[dict] = []
        for w in words:
            spk = _speaker_for_interval(w["start"], w["end"], turns)
            if spk != run_speaker and run_words:
                labeled.append({
                    "start": run_words[0]["start"],
                    "end": run_words[-1]["end"],
                    "text": "".join(x["word"] for x in run_words).strip(),
                    "speaker": run_speaker,
                })
                run_words = []
            run_speaker = spk
            run_words.append(w)
        if run_words:
            labeled.append({
                "start": run_words[0]["start"],
                "end": run_words[-1]["end"],
                "text": "".join(x["word"] for x in run_words).strip(),
                "speaker": run_speaker,
            })
    return labeled
