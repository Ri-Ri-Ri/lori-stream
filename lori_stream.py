#!/usr/bin/env python3
"""Lori Stream — streaming voice input for macOS.

Transcribes speech WHILE recording: audio is split into segments at natural
pauses, each finished segment goes to mlx-whisper immediately. On stop only
the short tail remains, so the paste arrives in ~1-2s instead of ~20s on
long dictations.

Standalone sibling of Lori (https://github.com/Ri-Ri-Ri/lori). If the stable
Lori is installed, they coexist: each has its own trigger, lock, log and
launchd agent; the model cache and the last-transcript file are shared, so
Lori's repaste hotkey re-pastes the latest transcript from either build.
"""
import datetime
import fcntl
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG_PATH = Path(__file__).parent / "lori-stream.log"

# Early logging — before any other imports
try:
    with open(LOG_PATH, "a") as _f:
        _f.write(f"[{time.strftime('%H:%M:%S')}] === START (pid={os.getpid()}) ===\n")
except Exception:
    pass

# reuse the stable Lori's model cache if it's installed — same model, downloaded once
_lori_models = Path.home() / ".lori" / "models"
os.environ["HF_HOME"] = str(_lori_models if _lori_models.exists() else Path(__file__).parent / "models")

import numpy as np
import sounddevice as sd
import mlx_whisper

CONFIG_PATH = Path(__file__).parent / "config.json"
MLX_WHISPER_MODEL = "mlx-community/whisper-medium-mlx"
MAX_LOG_SIZE = 1_000_000  # rotate to lori-stream.log.1 beyond this

# /tmp is world-writable: any local process could trigger the mic or
# symlink-attack the lock file — keep runtime files in a private per-user dir
RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "lori"
TOGGLE_FILE = RUNTIME_DIR / "toggle-stream"
LOCK_FILE = RUNTIME_DIR / "lori-stream.lock"
# shared with the stable Lori on purpose: its repaste hotkey re-pastes the
# latest transcript no matter which build produced it
LAST_TRANSCRIPT_FILE = RUNTIME_DIR / "last-transcript.txt"

DEFAULT_CONFIG = {
    "language": "en",
    "sample_rate": 16000,
    "min_volume": 0.01,
    "debounce_seconds": 0.3,
    "max_recording_seconds": 600,
    "min_segment_seconds": 8.0,
    "max_segment_seconds": 30.0,
    "silence_seconds": 0.6,
    "silence_amplitude": 0.008,
}

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_FINISHING = "finishing"

SELFTEST = False  # --selftest: feed a wav through the engine, print instead of paste


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_SIZE:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".1"))
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG)
    except Exception as e:
        log(f"Config error: {e} — using defaults")
        return dict(DEFAULT_CONFIG)


def notify(title, message=""):
    log(f"notify: {title} | {message}")
    if SELFTEST:
        return
    try:
        import UserNotifications as UN
        c = UN.UNUserNotificationCenter.currentNotificationCenter()
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(message or " ")
        content.setInterruptionLevel_(2)
        uid = f"loris{int(time.time()*1000)%99999}"
        req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(uid, content, None)
        c.addNotificationRequest_withCompletionHandler_(req, None)
    except Exception:
        pass


# one fixed identifier: Recording replaces itself with Transcribing and is
# removed after the paste, so dictations don't pile up in Notification Center.
# Distinct from the stable Lori's uid — both builds share one notification center
_STATUS_UID = "lori-stream-status"
STATUS_NOTIFICATIONS = True  # config "status_notifications", set in main()


def notify_status(title, message=""):
    if not STATUS_NOTIFICATIONS:
        return
    log(f"notify: {title} | {message}")
    if SELFTEST:
        return
    try:
        import UserNotifications as UN
        c = UN.UNUserNotificationCenter.currentNotificationCenter()
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(message or " ")
        content.setInterruptionLevel_(2)
        req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(_STATUS_UID, content, None)
        c.addNotificationRequest_withCompletionHandler_(req, None)
    except Exception:
        pass


def clear_status():
    if not STATUS_NOTIFICATIONS or SELFTEST:
        return
    try:
        import UserNotifications as UN
        c = UN.UNUserNotificationCenter.currentNotificationCenter()
        c.removePendingNotificationRequestsWithIdentifiers_([_STATUS_UID])
        c.removeDeliveredNotificationsWithIdentifiers_([_STATUS_UID])
    except Exception:
        pass


CHUNK_SIZE = 400  # Cursor/xterm.js hangs on large clipboard pastes

_REPEAT_LOOP = re.compile(r"((?:\S+\s){1,12}?)(?:\1){5,}")


def collapse_repeat_loops(text):
    return _REPEAT_LOOP.sub(lambda m: m.group(1) * 3, text)


# whisper was trained on videos that end with sign-off phrases — on trailing
# silence it hallucinates them; stripped only at the very end of a transcript
_TRAILING_HALLUCINATIONS = (
    "спасибо за внимание",
    "спасибо за просмотр",
    "продолжение следует",
    "субтитры сделал dimatorzok",
    "субтитры делал dimatorzok",
    "thanks for watching",
    "thank you for watching",
)

# blogger sign-offs carry arbitrary names («С вами был Игорь Негода»,
# «С вами был Юрий») — matched as a pattern, not exact phrases
_TRAILING_SIGNOFF = re.compile(r"\bс вами был[аи]?(?:[ ,]+[а-яё]+){1,3}$")


def strip_trailing_hallucinations(text):
    changed = True
    while changed:
        changed = False
        stripped = text.rstrip(" .,!…")
        low = stripped.lower()
        for phrase in _TRAILING_HALLUCINATIONS:
            if low.endswith(phrase):
                text = stripped[: len(stripped) - len(phrase)].rstrip(" .,!…")
                log(f"Trailing hallucination stripped: «{phrase}»")
                changed = True
                break
        if not changed:
            m = _TRAILING_SIGNOFF.search(low)
            if m:
                log(f"Trailing hallucination stripped: «{stripped[m.start():]}»")
                text = stripped[: m.start()].rstrip(" .,!…")
                changed = True
    return text


def _split_chunks(text, size):
    if len(text) <= size:
        return [text]
    chunks = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        cut = text.rfind(" ", 0, size)
        if cut == -1:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip(" ")
    return chunks


def _cmd_v():
    import Quartz
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(src, 9, down)
        Quartz.CGEventSetFlags(e, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, e)
        time.sleep(0.05)


def _snapshot_pasteboard(pb):
    saved = []
    for item in pb.pasteboardItems() or []:
        entry = {}
        for t in item.types():
            data = item.dataForType_(t)
            if data is not None:
                entry[str(t)] = data
        if entry:
            saved.append(entry)
    return saved


def _restore_pasteboard(pb, saved):
    from AppKit import NSPasteboardItem
    items = []
    for entry in saved:
        item = NSPasteboardItem.alloc().init()
        for t, data in entry.items():
            item.setData_forType_(data, t)
        items.append(item)
    if items:
        pb.clearContents()
        pb.writeObjects_(items)


def save_last_transcript(text):
    try:
        LAST_TRANSCRIPT_FILE.write_text(text)
        os.chmod(LAST_TRANSCRIPT_FILE, 0o600)
    except Exception as e:
        log(f"Last transcript save error: {e}")


def paste_text(text):
    text = " ".join(text.splitlines()).strip()
    text = "".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7F)
    if not text:
        log("Paste: empty text after sanitize")
        return

    save_last_transcript(text)

    if SELFTEST:
        log(f"[selftest] would paste {len(text)} chars: {text[:120]}...")
        return

    chunks = _split_chunks(text, CHUNK_SIZE)
    log(f"Paste ({len(text)} chars, {len(chunks)} chunk(s))")

    try:
        from AppKit import NSPasteboard
        pb = NSPasteboard.generalPasteboard()
        saved = _snapshot_pasteboard(pb)

        for i, chunk in enumerate(chunks):
            pb.clearContents()
            pb.setString_forType_(chunk, "public.utf8-plain-text")
            time.sleep(0.2)
            _cmd_v()
            if i < len(chunks) - 1:
                time.sleep(0.15)

        if saved:
            time.sleep(0.5)
            _restore_pasteboard(pb, saved)

        log("Paste: OK")
    except Exception as ex:
        log(f"Paste error: {ex}")
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True)
            time.sleep(0.3)
            _cmd_v()
            log("Paste: OK (pbcopy fallback)")
        except Exception as ex2:
            log(f"Paste fallback error: {ex2}")


class LoriStream:
    def __init__(self, config):
        self.config = config
        self.state = STATE_IDLE
        self._lock = threading.Lock()
        self._stream = None
        self._last_tap = 0.0
        self._auto_stop_timer = None

        sr = config["sample_rate"]
        self._min_seg = int(config["min_segment_seconds"] * sr)
        self._max_seg = int(config["max_segment_seconds"] * sr)
        self._silence_len = int(config["silence_seconds"] * sr)
        self._silence_amp = config["silence_amplitude"]

        # per-recording streaming state (reset in _start_recording)
        self._segments_q = None
        self._results = {}
        self._seq = 0
        self._cur_blocks = []
        self._cur_len = 0
        self._silence_run = 0
        self._worker = None
        self._had_audio_max = 0.0

        try:
            dev = sd.query_devices(kind='input')
            log(f"Mic: {dev['name']}")
        except Exception as e:
            log(f"Mic: {e}")

        lang = None if config["language"] == "auto" else config["language"]
        self._lang = lang
        log(f"Warming up model {MLX_WHISPER_MODEL} (language: {config['language']})...")
        mlx_whisper.transcribe(
            np.zeros(16000, dtype=np.float32),
            path_or_hf_repo=MLX_WHISPER_MODEL,
            language=lang,
        )
        log("Ready. Waiting for trigger.")

    def toggle(self):
        now = time.time()
        if now - self._last_tap < self.config["debounce_seconds"]:
            return
        self._last_tap = now

        with self._lock:
            if self.state == STATE_FINISHING:
                return
            elif self.state == STATE_RECORDING:
                self.state = STATE_FINISHING
                self._finalize_segment_locked(force=True)
                stream_to_stop = self._stream
                action = "stop"
            else:
                action = "start"

        if action == "stop":
            log("→ stop")
            notify_status("Lori Stream", "Transcribing…")
            if self._auto_stop_timer is not None:
                self._auto_stop_timer.cancel()
                self._auto_stop_timer = None
            try:
                stream_to_stop.stop()
                stream_to_stop.close()
            except Exception as e:
                log(f"Stop error: {e}")
            threading.Thread(target=self._finish, daemon=True).start()

        elif action == "start":
            log("→ start")
            self._start_recording()

    def _start_recording(self):
        with self._lock:
            self.state = STATE_RECORDING
            self._segments_q = queue.Queue()
            self._results = {}
            self._seq = 0
            self._cur_blocks = []
            self._cur_len = 0
            self._silence_run = 0
            self._had_audio_max = 0.0
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        def callback(indata, frames, time_info, status):
            with self._lock:
                if self.state != STATE_RECORDING:
                    return
                self._cur_blocks.append(indata.copy())
                self._cur_len += len(indata)
                blockmax = float(np.abs(indata).max())
                if blockmax > self._had_audio_max:
                    self._had_audio_max = blockmax
                if blockmax < self._silence_amp:
                    self._silence_run += len(indata)
                else:
                    self._silence_run = 0
                # cut at a natural pause once the segment is long enough;
                # hard cut at max length so one breathless monologue still streams
                if (self._cur_len >= self._min_seg and self._silence_run >= self._silence_len) \
                        or self._cur_len >= self._max_seg:
                    self._finalize_segment_locked()

        try:
            stream = sd.InputStream(
                samplerate=self.config["sample_rate"],
                channels=1,
                dtype="float32",
                callback=callback,
            )
            stream.start()
            self._stream = stream
            limit = self.config.get("max_recording_seconds", 600)
            self._auto_stop_timer = threading.Timer(limit, self._auto_stop)
            self._auto_stop_timer.daemon = True
            self._auto_stop_timer.start()
            log("Recording started (streaming)")
            notify_status("🎙 Lori Stream", "Recording…")
        except Exception as e:
            log(f"Recording error: {e}")
            with self._lock:
                self.state = STATE_IDLE
            if self._segments_q is not None:
                self._segments_q.put(None)

    def _finalize_segment_locked(self, force=False):
        # caller must hold self._lock; just hands blocks to the worker — cheap
        if not self._cur_blocks:
            return
        if not force and self._cur_len < self._silence_len:
            return
        self._segments_q.put((self._seq, self._cur_blocks))
        self._seq += 1
        self._cur_blocks = []
        self._cur_len = 0
        self._silence_run = 0

    def _worker_loop(self):
        q = self._segments_q
        while True:
            item = q.get()
            if item is None:
                return
            seq, blocks = item
            t0 = time.time()
            try:
                audio = np.concatenate(blocks, axis=0).flatten()
                duration = len(audio) / self.config["sample_rate"]
                max_vol = float(np.abs(audio).max())
                if max_vol < self.config.get("min_volume", 0.01):
                    self._results[seq] = ""
                    log(f"Segment {seq}: {duration:.1f}s too quiet, skipped")
                    continue
                if max_vol > 1.0:
                    p99 = float(np.percentile(np.abs(audio), 99))
                    scale = p99 * 3 if p99 > 0 else max_vol
                    audio = np.clip(audio, -scale, scale) / scale
                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=MLX_WHISPER_MODEL,
                    language=self._lang,
                    condition_on_previous_text=False,
                )
                text = collapse_repeat_loops(result["text"].strip())
                self._results[seq] = text
                log(f"Segment {seq}: {duration:.1f}s -> {len(text)} chars in {time.time()-t0:.1f}s")
            except Exception as e:
                self._results[seq] = ""
                log(f"Segment {seq} error: {e}")

    def _finish(self):
        t0 = time.time()
        try:
            self._segments_q.put(None)
            self._worker.join(timeout=120)
            texts = [self._results.get(i, "") for i in range(self._seq)]
            text = " ".join(t for t in texts if t).strip()
            text = collapse_repeat_loops(text)
            text = strip_trailing_hallucinations(text)
            log(f"Finish: {self._seq} segment(s), tail wait {time.time()-t0:.1f}s, {len(text)} chars")
            if text:
                paste_text(text)
            elif self._had_audio_max < self.config.get("min_volume", 0.01):
                notify("🔇 Lori Stream", "Too quiet — nothing transcribed")
            else:
                log("Silence (no text)")
        except Exception as e:
            log(f"Finish error: {e}")
        finally:
            clear_status()
            with self._lock:
                self.state = STATE_IDLE
            log("Done, waiting for trigger...")

    def _auto_stop(self):
        with self._lock:
            if self.state != STATE_RECORDING:
                return
        limit = self.config.get("max_recording_seconds", 600)
        log(f"Auto-stop: recording hit {limit}s limit")
        notify("⏱ Lori Stream: auto-stop", f"Recording hit the {limit // 60} min limit")
        self._last_tap = 0.0
        self.toggle()


_lock_fh = None


def acquire_lock():
    global _lock_fh
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    _lock_fh = open(LOCK_FILE, "a")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        sys.exit(0)


def watch_toggle_file(lori):
    while True:
        if TOGGLE_FILE.exists():
            try:
                TOGGLE_FILE.unlink()
            except Exception:
                pass
            lori.toggle()
        time.sleep(0.1)


def selftest(wav_path):
    """Feed a wav through the segmentation engine as if it came from the mic."""
    global SELFTEST
    SELFTEST = True
    import soundfile as sf
    config = load_config()
    lori = LoriStream(config)
    data, sr = sf.read(wav_path, dtype="float32")
    if data.ndim > 1:
        data = data[:, 0]
    if sr != config["sample_rate"]:
        log(f"[selftest] resampling {sr} -> {config['sample_rate']}")
        idx = np.linspace(0, len(data) - 1, int(len(data) * config["sample_rate"] / sr))
        data = np.interp(idx, np.arange(len(data)), data).astype("float32")
    log(f"[selftest] {wav_path}: {len(data)/config['sample_rate']:.1f}s")

    lori._start_recording_selftest()
    block = int(0.1 * config["sample_rate"])
    for i in range(0, len(data), block):
        lori._selftest_callback(data[i:i + block].reshape(-1, 1))
    with lori._lock:
        lori.state = STATE_FINISHING
        lori._finalize_segment_locked(force=True)
    lori._finish()


def _start_recording_selftest(self):
    with self._lock:
        self.state = STATE_RECORDING
        self._segments_q = queue.Queue()
        self._results = {}
        self._seq = 0
        self._cur_blocks = []
        self._cur_len = 0
        self._silence_run = 0
        self._had_audio_max = 0.0
    self._worker = threading.Thread(target=self._worker_loop, daemon=True)
    self._worker.start()


def _selftest_callback(self, indata):
    with self._lock:
        if self.state != STATE_RECORDING:
            return
        self._cur_blocks.append(indata.copy())
        self._cur_len += len(indata)
        blockmax = float(np.abs(indata).max())
        if blockmax > self._had_audio_max:
            self._had_audio_max = blockmax
        if blockmax < self._silence_amp:
            self._silence_run += len(indata)
        else:
            self._silence_run = 0
        if (self._cur_len >= self._min_seg and self._silence_run >= self._silence_len) \
                or self._cur_len >= self._max_seg:
            self._finalize_segment_locked()


LoriStream._start_recording_selftest = _start_recording_selftest
LoriStream._selftest_callback = _selftest_callback


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        return

    acquire_lock()
    try:
        import AppKit
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        log("NSApp: OK")
    except Exception as e:
        log(f"NSApp: unavailable ({e}), continuing without it")
        app = None

    config = load_config()
    global STATUS_NOTIFICATIONS
    STATUS_NOTIFICATIONS = config.get("status_notifications", True)

    while True:
        try:
            lori = LoriStream(config)
            break
        except Exception as e:
            log(f"Load error: {e}, retrying in 5s")
            time.sleep(5)

    threading.Thread(target=watch_toggle_file, args=(lori,), daemon=True).start()

    if app is not None:
        try:
            app.run()
        except Exception as e:
            log(f"NSApp.run() failed: {e}, keeping process alive via sleep")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
