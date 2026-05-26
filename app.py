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
import tempfile
import wave
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


def _write_wav(path, audio, sample_rate: int = SAMPLE_RATE) -> None:
    audio_int16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())


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
    Same speaker -> cosine distance < threshold (typically < 0.25).
    Used only in live mode. Post mode bypasses this entirely.
    """

    def __init__(self, threshold=0.22, max_speakers=8,
                 participant_prefix="Participant", your_name="You", mic_mode=False):
        self.speakers: list[tuple[np.ndarray, int, str]] = []
        self.threshold = threshold
        self.max_speakers = max_speakers
        self.prefix = participant_prefix
        self.your_name = your_name
        self.mic_mode = mic_mode

    def _mfcc(self, audio: np.ndarray, n_mfcc: int = 20) -> np.ndarray | None:
        sr = SAMPLE_RATE
        if len(audio) < 1024:
            return None

        audio = np.concatenate([[audio[0]], audio[1:] - 0.97 * audio[:-1]])

        frame_len, hop_len, n_fft = 400, 160, 512
        n_frames = 1 + (len(audio) - frame_len) // hop_len
        if n_frames < 1:
            return None

        idx = (np.arange(frame_len)[None, :] +
               np.arange(n_frames)[:, None] * hop_len)
        frames = audio[idx] * np.hamming(frame_len)

        spec = np.abs(np.fft.rfft(frames, n=n_fft)) ** 2 / n_fft

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

        n = log_mel.shape[1]
        k = np.arange(n_mfcc)
        dct_mat = np.cos(np.pi / n * np.outer(k, np.arange(0.5, n + 0.5)))
        mfcc = np.dot(log_mel, dct_mat.T) * np.sqrt(2.0 / n)

        mfcc -= mfcc.mean(axis=0)
        feat = mfcc.mean(axis=0)

        norm = np.linalg.norm(feat)
        return feat / norm if norm > 0 else None

    @staticmethod
    def _dist(a: np.ndarray, b: np.ndarray) -> float:
        return float(1.0 - np.dot(a, b))

    def _make_name(self, index: int) -> str:
        if self.mic_mode:
            return self.your_name if index == 0 else f"{self.your_name} ({index + 1})"
        return f"{self.prefix} {index + 1}"

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
            idx = next(i for i, s in enumerate(self.speakers) if s[2] == closest)
            acc, cnt, name = self.speakers[idx]
            cnt += 1
            acc = acc + (feat - acc) / cnt
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
    def __init__(self, device, channels: int, label: str, out_queue: queue.Queue,
                 record_to: list | None = None, record_only: bool = False):
        self.device = device
        self.channels = channels
        self.label = label
        self.out_queue = out_queue
        self._record = record_to  # if set, raw chunks are appended here for post-processing
        self._record_only = record_only  # post mode: only record, skip live VAD/transcription
        self._stream = None
        self._buf = np.array([], dtype=np.float32)
        self._in_speech = False
        self._silence_samples = 0
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        audio = (indata.mean(axis=1) if indata.shape[1] > 1
                 else indata[:, 0]).astype(np.float32)
        if self._record is not None:
            self._record.append(audio.copy())
        if self._record_only:
            return
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
                 locale: dict = None, meeting_name: str = "",
                 speaker_mode: str = "live", fmt: str = "prose"):
        self.model_size = model_size
        self.meeting_lang = meeting_lang
        self.meeting_name = meeting_name
        self.locale = locale or {}
        self.speaker_mode = speaker_mode  # "live" or "post"
        self.fmt = fmt  # post mode transcript format: "prose" or "timestamped"
        self._your_name = "You"
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
        # post mode: raw audio recorded per source, mixed into one WAV at stop
        self._mic_audio: list[np.ndarray] = []
        self._sys_audio: list[np.ndarray] = []

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
        self._mic_audio = []
        self._sys_audio = []
        self._your_name = your_name

        if self.speaker_mode == "post":
            # Record only — both sources are mixed and diarized after stop.
            self._mic_stream = AudioStream(mic_device, 1, "__MIC__", self._seg_queue,
                                           record_to=self._mic_audio, record_only=True)
            self._mic_stream.start()
            if system_device is not None:
                ch = min(2, sd.query_devices(system_device)["max_input_channels"])
                self._sys_stream = AudioStream(system_device, ch, "__SYSTEM__",
                                               self._seg_queue,
                                               record_to=self._sys_audio, record_only=True)
                self._sys_stream.start()
            return

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
        if self.speaker_mode == "post":
            return self._save_post()
        return self._save_live()

    def _save_live(self) -> Path | None:
        if not self.transcript:
            return None
        folder = get_meeting_folder(self.meeting_name, self.session_start)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "transcript.md"
        path.write_text(self._markdown(), encoding="utf-8")
        return path

    def _save_post(self) -> Path | None:
        mic = (np.concatenate(self._mic_audio) if self._mic_audio
               else np.array([], dtype=np.float32))
        sys_audio = (np.concatenate(self._sys_audio) if self._sys_audio
                     else np.array([], dtype=np.float32))
        if mic.size == 0 and sys_audio.size == 0:
            return None

        # Streams start together but may end a few samples apart; pad to equal length.
        n = max(mic.size, sys_audio.size)
        mic = np.pad(mic, (0, n - mic.size))
        sys_audio = np.pad(sys_audio, (0, n - sys_audio.size))

        # Mixed track feeds transcription so all speech (local + remote) is captured.
        mixed = mic + sys_audio
        peak = np.abs(mixed).max()
        if peak > 1.0:
            mixed = mixed / peak

        folder = get_meeting_folder(self.meeting_name, self.session_start)
        folder.mkdir(parents=True, exist_ok=True)
        wav_path = folder / "meeting_audio.wav"
        _write_wav(wav_path, mixed)

        print(f"\nAudio saved. Running transcription and speaker diarization...")
        print("(this may take several minutes depending on recording length)\n")

        # Channel-aware diarization: with a separate system channel, diarize only
        # the remote audio and label the local mic as "You" (avoids the local
        # speaker being merged into a remote cluster). Without a system channel,
        # fall back to diarizing the whole recording.
        # Channel-aware needs a real system channel. If BlackHole captured nothing
        # (no Multi-Output Device, or speakers instead of headphones), the mic holds
        # everything — fall back to plain diarization so the local speaker is still
        # detected (just not guaranteed as a distinct "You").
        speaker_turns = None
        sys_rms = float(np.sqrt(np.mean(sys_audio ** 2))) if sys_audio.size else 0.0
        if sys_rms > SILENCE_THRESHOLD:
            from diarize import (diarize as run_diarize,
                                  mic_dominant_blocks, combine_turns)
            sys_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
            _write_wav(sys_wav, sys_audio)
            try:
                sys_turns = run_diarize(sys_wav)
            finally:
                sys_wav.unlink(missing_ok=True)
            you_blocks = mic_dominant_blocks(mic, sys_audio, SAMPLE_RATE,
                                             threshold=SILENCE_THRESHOLD)
            speaker_turns = combine_turns(sys_turns, you_blocks,
                                          you_label=self._your_name)
        else:
            print("System channel is silent — diarizing the microphone recording "
                  "directly. For the local speaker to be labeled separately, route "
                  "system audio to BlackHole and use headphones.")

        from transcribe_file import transcribe_file as _transcribe_file
        return _transcribe_file(
            str(wav_path),
            model_size=self.model_size,
            language=self.meeting_lang,
            meeting_name=self.meeting_name,
            fmt=self.fmt,
            diarize=True,
            speaker_turns=speaker_turns,
            output_folder=folder,
            session_start=self.session_start,
        )

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


from folders import get_meeting_folder  # noqa: F401 (re-exported for callers)


# ─── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Meeting Transcriber")
    parser.add_argument("--ui-lang", default="en", help="UI language code (e.g. en, pt)")
    parser.add_argument("--meeting-lang", default="en",
                        help="Meeting language code for Whisper (e.g. en, pt, es)")
    parser.add_argument("--meeting-name", default="", help="Optional meeting name")
    parser.add_argument("--speaker-mode", default="live", choices=["live", "post"],
                        help="live: MFCC labels in real time; post: record and diarize after")
    parser.add_argument("--model", default="",
                        help="Whisper model for post-mode transcription (default: large-v3)")
    parser.add_argument("--format", default="prose", choices=["prose", "timestamped"],
                        help="post-mode transcript format (default: prose)")
    args = parser.parse_args()

    s = load_locale(args.ui_lang)

    if args.speaker_mode == "post":
        model_size = args.model or "large-v3"
    else:
        model_size = "small.en" if args.meeting_lang == "en" else "small"

    session = MeetingSession(model_size=model_size, meeting_lang=args.meeting_lang,
                             locale=s, meeting_name=args.meeting_name,
                             speaker_mode=args.speaker_mode, fmt=args.format)

    if args.speaker_mode == "post":
        print(f"High accuracy mode: recording audio. Transcription with "
              f"'{model_size}' and speaker diarization run after you stop.\n")
    else:
        session.load_model()

    mic_device, system_device = session.find_devices()

    print(s.get("devices_title", "Detected audio devices:"))
    print(f"  {s.get('device_mic', 'Microphone')} : #{mic_device} "
          f"{sd.query_devices(mic_device)['name']}")
    if system_device is not None:
        print(f"  {s.get('device_system', 'System audio')} : #{system_device} "
              f"{sd.query_devices(system_device)['name']}")
    else:
        print(f"  {s.get('blackhole_missing', 'BlackHole not found — microphone only will be captured')}")
    print()

    if args.speaker_mode == "post" and system_device is None:
        print("Note: no BlackHole device found. Recording microphone only.")
        print()

    print(s.get("press_enter", "Press ENTER to start recording..."))
    input()

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

    signal.pause()


if __name__ == "__main__":
    main()
