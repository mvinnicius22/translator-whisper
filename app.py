#!/usr/bin/env python3
"""
Meeting Transcriber — Real-time meeting transcription for macOS
Uses OpenAI Whisper + BlackHole for system audio capture
"""

import argparse
import json
import re
import threading
import queue
import signal
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import whisper

# ─── Audio constants ────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
CHUNK_SIZE = int(SAMPLE_RATE * 0.5)
SILENCE_THRESHOLD = 0.008
SILENCE_DURATION = 1.2
MAX_SEGMENT_DURATION = 20.0
MIN_SEGMENT_DURATION = 0.6


# ─── Locale loading ─────────────────────────────────────────────────────────────

def load_locale(ui_lang: str) -> dict:
    locale_path = Path(__file__).parent / "locales" / f"{ui_lang}.json"
    with open(locale_path, encoding="utf-8") as f:
        data = json.load(f)
    return data["app"]


# ─── Speaker identification ─────────────────────────────────────────────────────

class SpeakerIdentifier:
    """
    Speaker identification using MFCC features + cosine distance.
    Same speaker → cosine distance < threshold (typically < 0.25).
    """

    def __init__(self, threshold=0.22, max_speakers=8,
                 participant_prefix="Participant", your_name="You", mic_mode=False):
        # Each entry: (accumulated MFCC sum, count, name) for running mean
        self.speakers: list[tuple[np.ndarray, int, str]] = []
        self.threshold = threshold
        self.max_speakers = max_speakers
        self.prefix = participant_prefix
        self.your_name = your_name
        self.mic_mode = mic_mode

    # ── MFCC computation (pure numpy, no extra deps) ──────────────────────────

    def _mfcc(self, audio: np.ndarray, n_mfcc: int = 20) -> np.ndarray | None:
        sr = SAMPLE_RATE
        if len(audio) < 1024:
            return None

        # Pre-emphasis
        audio = np.concatenate([[audio[0]], audio[1:] - 0.97 * audio[:-1]])

        # Framing: 25 ms frames, 10 ms hop
        frame_len, hop_len, n_fft = 400, 160, 512
        n_frames = 1 + (len(audio) - frame_len) // hop_len
        if n_frames < 1:
            return None

        idx = (np.arange(frame_len)[None, :] +
               np.arange(n_frames)[:, None] * hop_len)
        frames = audio[idx] * np.hamming(frame_len)

        # Power spectrum
        spec = np.abs(np.fft.rfft(frames, n=n_fft)) ** 2 / n_fft

        # Mel filterbank (40 filters, 0 Hz – Nyquist)
        n_mels = 40
        mel = lambda f: 2595 * np.log10(1 + f / 700)
        imel = lambda m: 700 * (10 ** (m / 2595) - 1)
        mel_pts = np.linspace(mel(0), mel(sr / 2), n_mels + 2)
        bins = np.floor((n_fft + 1) * imel(mel_pts) / sr).astype(int)

        fbank = np.zeros((n_mels, n_fft // 2 + 1))
        for m in range(1, n_mels + 1):
            lo, c, hi = bins[m - 1], bins[m], bins[m + 1]
            if c > lo:
                fbank[m - 1, lo:c] = (np.arange(lo, c) - lo) / (c - lo)
            if hi > c:
                fbank[m - 1, c:hi] = (hi - np.arange(c, hi)) / (hi - c)

        log_mel = np.log(np.maximum(np.dot(spec, fbank.T), 1e-10))

        # DCT-II via numpy (approximation)
        n = log_mel.shape[1]
        k = np.arange(n_mfcc)
        dct_mat = np.cos(np.pi / n * np.outer(k, np.arange(0.5, n + 0.5)))
        mfcc = np.dot(log_mel, dct_mat.T) * np.sqrt(2.0 / n)

        # Cepstral mean normalisation + global mean
        mfcc -= mfcc.mean(axis=0)
        feat = mfcc.mean(axis=0)

        # L2-normalise for cosine distance via dot product
        norm = np.linalg.norm(feat)
        return feat / norm if norm > 0 else None

    # ── cosine distance (0 = identical, 2 = opposite) ─────────────────────────

    @staticmethod
    def _dist(a: np.ndarray, b: np.ndarray) -> float:
        return float(1.0 - np.dot(a, b))  # both L2-normalised

    # ── label helpers ─────────────────────────────────────────────────────────

    def _make_name(self, index: int) -> str:
        if self.mic_mode:
            return self.your_name if index == 0 else f"{self.your_name} ({index + 1})"
        return f"{self.prefix} {index + 1}"

    # ── public API ────────────────────────────────────────────────────────────

    def identify(self, audio: np.ndarray) -> str:
        feat = self._mfcc(audio)
        first = self._make_name(0)
        if feat is None:
            return first

        if not self.speakers:
            self.speakers.append((feat.copy(), 1, first))
            return first

        dists = [(self._dist(feat, s[0]), s[2]) for s in self.speakers]
        min_dist, closest = min(dists)

        if min_dist < self.threshold:
            # Update running mean for this speaker
            idx = next(i for i, s in enumerate(self.speakers) if s[2] == closest)
            acc, cnt, name = self.speakers[idx]
            cnt += 1
            acc = acc + (feat - acc) / cnt  # incremental mean (already normalised per step)
            norm = np.linalg.norm(acc)
            self.speakers[idx] = (acc / norm if norm > 0 else acc, cnt, name)
            return closest

        if len(self.speakers) < self.max_speakers:
            name = self._make_name(len(self.speakers))
            self.speakers.append((feat.copy(), 1, name))
            return name

        return closest


# ─── Audio stream with VAD ──────────────────────────────────────────────────────

class AudioStream:
    def __init__(self, device, channels: int, label: str, out_queue: queue.Queue):
        self.device = device
        self.channels = channels
        self.label = label
        self.out_queue = out_queue
        self._stream = None
        self._buf = np.array([], dtype=np.float32)
        self._in_speech = False
        self._silence_samples = 0
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        audio = (indata.mean(axis=1) if indata.shape[1] > 1
                 else indata[:, 0]).astype(np.float32)
        with self._lock:
            self._vad(audio)

    def _vad(self, audio: np.ndarray):
        is_speech = np.abs(audio).mean() > SILENCE_THRESHOLD
        if is_speech:
            self._buf = np.append(self._buf, audio)
            self._in_speech = True
            self._silence_samples = 0
        elif self._in_speech:
            self._buf = np.append(self._buf, audio)
            self._silence_samples += len(audio)
            if (self._silence_samples >= int(SILENCE_DURATION * SAMPLE_RATE) or
                    len(self._buf) >= int(MAX_SEGMENT_DURATION * SAMPLE_RATE)):
                self._flush()

    def _flush(self):
        if len(self._buf) >= int(MIN_SEGMENT_DURATION * SAMPLE_RATE):
            self.out_queue.put((self.label, self._buf.copy()))
        self._buf = np.array([], dtype=np.float32)
        self._in_speech = False
        self._silence_samples = 0

    def start(self):
        self._stream = sd.InputStream(
            device=self.device, channels=self.channels,
            samplerate=SAMPLE_RATE, blocksize=CHUNK_SIZE,
            dtype="float32", callback=self._callback)
        self._stream.start()

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            self._flush()


# ─── Meeting session ────────────────────────────────────────────────────────────

HALLUCINATIONS = {
    "", ".", "..", "...", "uh", "um", "you",
    "thank you", "thanks for watching", "please subscribe",
    "thanks", "bye", "okay",
}


class MeetingSession:
    def __init__(self, model_size: str = "small.en", meeting_lang: str = "en",
                 locale: dict = None, meeting_name: str = ""):
        self.model_size = model_size
        self.meeting_lang = meeting_lang
        self.meeting_name = meeting_name
        self.locale = locale or {}
        self.model = None
        self.transcript: list[dict] = []
        self.session_start: datetime | None = None
        self.is_running = False
        self._seg_queue: queue.Queue = queue.Queue()
        self._mic_stream: AudioStream | None = None
        self._sys_stream: AudioStream | None = None
        self._worker: threading.Thread | None = None
        self._participant_prefix = (self.locale or {}).get("doc_participants_label", "Participant")
        self._mic_diarizer: SpeakerIdentifier | None = None
        self._sys_diarizer: SpeakerIdentifier | None = None

    def load_model(self):
        s = self.locale
        print(s.get("loading_model", "Loading Whisper model ({model})...").format(
            model=self.model_size))
        self.model = whisper.load_model(self.model_size)
        print(s.get("model_loaded", "Model loaded!") + "\n")

    @staticmethod
    def find_devices() -> tuple[int | None, int | None]:
        mic_device = sd.default.device[0]
        system_device = None
        for i, dev in enumerate(sd.query_devices()):
            if "blackhole" in dev["name"].lower() and dev["max_input_channels"] > 0:
                system_device = i
                break
        return mic_device, system_device

    def start(self, mic_device, system_device, your_name: str = "You"):
        self.session_start = datetime.now()
        self.is_running = True
        self.transcript = []
        self._mic_diarizer = SpeakerIdentifier(
            threshold=0.38, mic_mode=True,
            your_name=your_name, participant_prefix=self._participant_prefix)
        self._sys_diarizer = SpeakerIdentifier(
            threshold=0.22, participant_prefix=self._participant_prefix)

        self._mic_stream = AudioStream(mic_device, 1, "__MIC__", self._seg_queue)
        self._mic_stream.start()

        if system_device is not None:
            ch = min(2, sd.query_devices(system_device)["max_input_channels"])
            self._sys_stream = AudioStream(system_device, ch, "__SYSTEM__",
                                           self._seg_queue)
            self._sys_stream.start()

        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _loop(self):
        while self.is_running or not self._seg_queue.empty():
            try:
                label, audio = self._seg_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if label == "__MIC__":
                label = self._mic_diarizer.identify(audio)
            elif label == "__SYSTEM__":
                label = self._sys_diarizer.identify(audio)
            text = self._transcribe(audio)
            if text and text.lower().strip(".").strip() not in HALLUCINATIONS:
                entry = {
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "speaker": label,
                    "text": text.strip(),
                }
                self.transcript.append(entry)
                print(f"[{entry['ts']}] {label}: {entry['text']}")

    def _transcribe(self, audio: np.ndarray) -> str:
        if self.model is None:
            return ""
        peak = np.abs(audio).max()
        if peak > 0:
            audio = audio / peak * 0.9
        try:
            result = self.model.transcribe(
                audio, language=self.meeting_lang, task="transcribe",
                condition_on_previous_text=False,
                no_speech_threshold=0.6, logprob_threshold=-1.0)
            return result["text"]
        except Exception:
            return ""

    def stop(self) -> Path | None:
        self.is_running = False
        if self._mic_stream:
            self._mic_stream.stop()
        if self._sys_stream:
            self._sys_stream.stop()
        if self._worker:
            self._worker.join(timeout=60)
        return self._save()

    def _save(self) -> Path | None:
        if not self.transcript:
            return None
        folder = get_meeting_folder(self.meeting_name, self.session_start)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "transcript.md"
        path.write_text(self._markdown(), encoding="utf-8")
        return path

    def _markdown(self) -> str:
        s = self.locale
        end = datetime.now()
        delta = int((end - self.session_start).total_seconds())
        duration = f"{delta // 3600}h {(delta % 3600) // 60}m {delta % 60}s"
        speakers = list(dict.fromkeys(e["speaker"] for e in self.transcript))
        doc_title = s.get("doc_title", "Meeting")
        doc_info = s.get("doc_info", "Info")
        doc_date = s.get("doc_date", "Date")
        doc_start = s.get("doc_start", "Start")
        doc_end = s.get("doc_end", "End")
        doc_duration = s.get("doc_duration", "Duration")
        doc_participants = s.get("doc_participants", "Participants")
        doc_transcript = s.get("doc_transcript", "Transcript")
        doc_generated_tpl = s.get("doc_generated", "Auto-generated on {date} at {time}")
        doc_generated = doc_generated_tpl.format(
            date=end.strftime("%d/%m/%Y"),
            time=end.strftime("%H:%M"),
        )
        lines = [
            f"# {doc_title} — {self.session_start.strftime('%d/%m/%Y')}",
            "",
            f"## {doc_info}",
            "",
            f"| | |",
            f"|---|---|",
            f"| **{doc_date}** | {self.session_start.strftime('%d/%m/%Y')} |",
            f"| **{doc_start}** | {self.session_start.strftime('%H:%M')} |",
            f"| **{doc_end}** | {end.strftime('%H:%M')} |",
            f"| **{doc_duration}** | {duration} |",
            f"| **{doc_participants}** | {', '.join(speakers)} |",
            "",
            f"## {doc_transcript}",
            "",
        ]
        prev = None
        for e in self.transcript:
            if e["speaker"] != prev:
                lines.append(f"**{e['speaker']}** *({e['ts']})*: {e['text']}")
                prev = e["speaker"]
            else:
                lines[-1] += f" {e['text']}"
        lines += [
            "",
            "---",
            f"*{doc_generated}*",
        ]
        return "\n".join(lines)


# ─── Meeting folder helper ──────────────────────────────────────────────────────

def get_meeting_folder(meeting_name: str, session_start: datetime) -> Path:
    """Build the meeting folder path based on settings."""
    settings_path = Path(__file__).parent / "settings.json"
    folder_structure = "flat"
    if settings_path.exists():
        with open(settings_path, encoding="utf-8") as f:
            folder_structure = json.load(f).get("folder_structure", "flat")

    # Sanitize name: lowercase, spaces to hyphens, keep alphanumeric and hyphens only
    safe_name = re.sub(r"[^a-z0-9\-]", "", meeting_name.lower().replace(" ", "-"))
    if not safe_name:
        safe_name = "meeting"

    date_str = session_start.strftime("%Y%m%d")
    time_str = session_start.strftime("%H%M")

    base = Path.home() / "Documents" / "Meetings"

    if folder_structure == "daily":
        folder = base / date_str / f"{time_str}_{safe_name}"
    else:
        folder = base / f"{date_str}_{time_str}_{safe_name}"

    return folder


# ─── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Meeting Transcriber")
    parser.add_argument("--ui-lang", default="en", help="UI language code (e.g. en, pt)")
    parser.add_argument("--meeting-lang", default="en",
                        help="Meeting language code for Whisper (e.g. en, pt, es)")
    parser.add_argument("--meeting-name", default="", help="Optional meeting name")
    args = parser.parse_args()

    s = load_locale(args.ui_lang)

    # Pick a model name based on meeting language (non-English uses multilingual model)
    model_size = "small.en" if args.meeting_lang == "en" else "small"

    session = MeetingSession(model_size=model_size, meeting_lang=args.meeting_lang,
                             locale=s, meeting_name=args.meeting_name)
    session.load_model()

    mic_device, system_device = session.find_devices()

    print(s.get("devices_title", "Detected audio devices:"))
    print(f"  {s.get('device_mic', 'Microphone')} : #{mic_device} — "
          f"{sd.query_devices(mic_device)['name']}")
    if system_device is not None:
        print(f"  {s.get('device_system', 'System audio')} : #{system_device} — "
              f"{sd.query_devices(system_device)['name']}")
    else:
        print(f"  {s.get('blackhole_missing', 'BlackHole not found — microphone only will be captured')}")
    print()

    print(s.get("press_enter", "Press ENTER to start recording..."))
    input()

    # Determine the label for the local microphone user
    your_name = "You" if args.ui_lang == "en" else "Você"
    session.start(mic_device, system_device, your_name=your_name)

    start_str = session.session_start.strftime("%H:%M")
    recording_msg = s.get("recording_since",
                           "Recording since {time} — press Ctrl+C to stop and save")
    print(f"🔴 {recording_msg.format(time=start_str)}\n")

    def on_stop(sig, frame):
        print(s.get("stopping", "\nStopping transcription..."))
        path = session.stop()
        if path:
            saved_msg = s.get("saved", "✅ Document saved at: {path}")
            print(saved_msg.format(path=path))
        else:
            print(s.get("no_content", "No content was transcribed."))
        sys.exit(0)

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    # Keep the process alive
    signal.pause()


if __name__ == "__main__":
    main()
