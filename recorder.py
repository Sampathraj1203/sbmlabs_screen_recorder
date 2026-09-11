"""SBM Labs Screen Recorder -- records the Windows desktop (and a microphone) to MP4.

Desktop video plus optional audio from any input device Windows exposes
(microphone, USB audio, Stereo Mix ...).  Whole desktop or one monitor.

    python recorder.py                         # GUI (Start / Stop, Ctrl+Shift+F9 hotkey)
    python recorder.py --cli                   # record until Enter / Ctrl+C
    python recorder.py --cli --audio default   # with the first microphone found
    python recorder.py --list-audio            # show input devices

Engine: ffmpeg (gdigrab video + dshow audio -> libx264 / aac MP4) when it is
on PATH; it draws the mouse cursor and encodes in real time.  Without ffmpeg
it falls back to Pillow ImageGrab + OpenCV VideoWriter (video only, no cursor).

GUI needs `customtkinter` (pip install customtkinter); the CLI does not.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

APP_NAME = "SBM Labs Screen Recorder"
DEFAULT_OUT_DIR = Path.home() / "Videos" / "sbmlabs_screen_recorder"
SETTINGS_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "sbmlabs_screen_recorder" / "settings.json"

# name -> (x264 crf, x264 preset)   lower crf = better picture, bigger file
QUALITY_PRESETS = {
    "Small file": (28, "veryfast"),
    "Balanced": (23, "veryfast"),
    "High": (18, "faster"),
}
FPS_CHOICES = (15, 24, 30, 60)
# name -> (aac bitrate, sample rate)
AUDIO_PRESETS = {
    "Standard": ("160k", 44100),
    "High": ("320k", 48000),
}
AUDIO_BUFFER_MS = 50   # dshow capture buffer; small keeps audio tight against the video
MIC_GAIN_MAX = 30       # dB
MIC_GAIN_DEFAULT = 20   # dB -- typical USB mics on Windows come in 15-25 dB too quiet
NO_AUDIO = "No audio"

HOTKEY_ID = 1
HOTKEY_LABEL = "Ctrl+Shift+F9"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_F9 = 0x78
WM_HOTKEY = 0x0312
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Region = tuple[int, int, int, int]  # left, top, width, height


# ---------------------------------------------------------------- monitors --
def _make_dpi_aware() -> None:
    """Physical pixels everywhere, so monitor rects match what gdigrab sees."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def list_monitors() -> list[Region]:
    """[(left, top, width, height), ...] for every attached monitor."""
    rects: list[Region] = []
    proc_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def _cb(_hmon, _hdc, prect, _lparam):
        r = prect.contents
        rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_t(_cb), 0)
    rects.sort(key=lambda r: (r[0], r[1]))
    return rects


def virtual_desktop() -> Region:
    u = ctypes.windll.user32
    return u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)


def capture_targets() -> list[tuple[str, Region | None]]:
    """Dropdown entries: 'Full desktop' (None = whole virtual screen) then each monitor."""
    mons = list_monitors()
    targets: list[tuple[str, Region | None]] = [("Full desktop", None)]
    if len(mons) > 1:
        for i, (x, y, w, h) in enumerate(mons, 1):
            targets.append((f"Monitor {i}  ({w}x{h} @ {x},{y})", (x, y, w, h)))
    return targets


def new_output_path(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"sbmlabs_rec_{datetime.now():%Y%m%d_%H%M%S}.mp4"


# ---------------------------------------------------------------- audio -----
@dataclass
class AudioDevice:
    name: str          # friendly name shown to the user
    alt: str = ""      # DirectShow "@device_cm_{...}" id, stable even with duplicate names

    @property
    def dshow_input(self) -> str:
        return f"audio={self.alt or self.name}"


def list_audio_devices(ffmpeg: str | None) -> list[AudioDevice]:
    """DirectShow audio inputs as ffmpeg reports them (microphones, Stereo Mix, ...)."""
    if not ffmpeg:
        return []
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=NO_WINDOW,
        ).stderr
    except Exception:
        return []
    devices: list[AudioDevice] = []
    current: AudioDevice | None = None
    section = ""
    for line in out.splitlines():
        if "DirectShow audio devices" in line:      # older ffmpeg: section headers
            section = "audio"
            continue
        if "DirectShow video devices" in line:
            section = "video"
            continue
        m = re.search(r'Alternative name\s+"(.+)"', line)
        if m:
            if current:
                current.alt = m.group(1)
                current = None
            continue
        m = re.search(r'"(.+)"\s+\((audio|video|none)\)\s*$', line)   # ffmpeg >= 4.3: "(name)" (kind)
        if m:
            kind = m.group(2)
        else:
            m = re.search(r'\]\s+"(.+)"\s*$', line)                     # older ffmpeg: name only
            kind = section
        if m:
            current = AudioDevice(m.group(1)) if kind == "audio" else None
            if current:
                devices.append(current)
    return devices


@dataclass
class AudioSettings:
    """Everything the audio path needs: which device, how loud, how to encode."""
    device: AudioDevice
    quality: str = "Standard"
    gain_db: float = MIC_GAIN_DEFAULT
    auto_level: bool = True

    @property
    def filter(self) -> str:
        """ffmpeg -af chain applied to the mic before encoding (and before the test meter).

        auto_level: speechnorm raises quiet speech by up to `gain_db` and pulls loud
        parts down, targeting a 0.9 peak.  Otherwise a plain fixed gain.  A limiter
        at the end keeps either mode from clipping."""
        gain = max(0.0, min(float(MIC_GAIN_MAX), self.gain_db))
        parts = []
        if self.auto_level:
            parts.append(f"speechnorm=e={10 ** (gain / 20):.2f}:p=0.9:l=1")
        elif gain > 0:
            parts.append(f"volume={gain:g}dB")
        parts.append("alimiter=limit=0.95:level=0")
        return ",".join(parts)

    @property
    def label(self) -> str:
        return f"+{self.gain_db:.0f} dB{' auto' if self.auto_level else ''}{', HQ' if self.quality == 'High' else ''}"


def find_audio_device(devices: list[AudioDevice], wanted: str | None) -> AudioDevice | None:
    """'default' -> first device; otherwise exact, then case-insensitive substring match on the name."""
    if not wanted or wanted == NO_AUDIO or not devices:
        return None
    if wanted.lower() == "default":
        return devices[0]
    for d in devices:
        if d.name == wanted:
            return d
    for d in devices:
        if wanted.lower() in d.name.lower():
            return d
    return None


# ---------------------------------------------------------------- engines ---
class FfmpegRecorder:
    """gdigrab (+ dshow audio) -> libx264 / aac.  Stopped by sending 'q' so the MP4 is finalised."""

    name = "ffmpeg (gdigrab + libx264 / aac)"
    supports_audio = True

    def __init__(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg
        self.proc: subprocess.Popen | None = None
        self.path: Path | None = None

    def start(self, path: Path, fps: int, quality: str, region: Region | None,
              audio: AudioSettings | None = None) -> None:
        crf, preset = QUALITY_PRESETS[quality]
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if audio is not None:
            cmd += ["-f", "dshow", "-audio_buffer_size", str(AUDIO_BUFFER_MS),
                    "-thread_queue_size", "1024", "-rtbufsize", "64M", "-i", audio.device.dshow_input]
        cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1",
                "-rtbufsize", "256M", "-thread_queue_size", "512"]
        if region is not None:
            x, y, w, h = region
            cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
        cmd += ["-i", "desktop"]
        if audio is not None:
            bitrate, sample_rate = AUDIO_PRESETS[audio.quality]
            cmd += ["-map", "1:v:0", "-map", "0:a:0",
                    "-c:a", "aac", "-b:a", bitrate, "-ar", str(sample_rate),
                    "-af", f"aresample=async=1,{audio.filter}"]
        cmd += [
            # yuv420p needs even dimensions; odd desktop sizes get cropped by 1px
            "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(path),
        ]
        self.path = path
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        # ffmpeg exits within a second if the args / devices are wrong; catch that early
        time.sleep(1.0)
        if self.proc.poll() is not None:
            err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""
            self.proc = None
            raise RuntimeError(f"ffmpeg failed to start: {err or 'unknown error'}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> Path:
        proc, self.proc = self.proc, None
        if proc is None:
            raise RuntimeError("not recording")
        err = b""
        if proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except OSError:
                pass
            try:
                _, err = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.terminate()
                _, err = proc.communicate(timeout=5)
        else:
            _, err = proc.communicate()
        if proc.returncode not in (0, 255) or not (self.path and self.path.exists()):
            raise RuntimeError(f"ffmpeg exited with {proc.returncode}: {err.decode(errors='replace').strip()}")
        return self.path


class PillowRecorder:
    """Fallback: ImageGrab frames written by OpenCV at a real-time paced fps.  Video only."""

    name = "Pillow ImageGrab + OpenCV (no cursor, no audio)"
    supports_audio = False

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: Exception | None = None
        self.path: Path | None = None

    def start(self, path: Path, fps: int, quality: str, region: Region | None,
              audio: AudioSettings | None = None) -> None:
        import cv2  # noqa: F401  -- fail here, not inside the thread
        from PIL import ImageGrab  # noqa: F401

        self.path = path
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, args=(path, fps, region), daemon=True)
        self._thread.start()
        time.sleep(0.5)
        if self._error:
            raise RuntimeError(f"capture failed: {self._error}")

    def _run(self, path: Path, fps: int, region: Region | None) -> None:
        import cv2
        import numpy as np
        from PIL import ImageGrab

        try:
            x, y, w, h = virtual_desktop() if region is None else region
            w, h = w - (w % 2), h - (h % 2)
            bbox = (x, y, x + w, y + h)
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
            if not writer.isOpened():
                raise RuntimeError(f"could not open {path} for writing")
            start = time.monotonic()
            written = 0
            frame_dt = 1.0 / fps
            try:
                while not self._stop.is_set():
                    img = ImageGrab.grab(bbox=bbox, all_screens=True)
                    frame = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
                    # keep wall-clock timing: repeat the frame if grabbing fell behind
                    due = int((time.monotonic() - start) * fps) + 1
                    for _ in range(max(1, due - written)):
                        writer.write(frame)
                    written = max(written + 1, due)
                    sleep_for = start + written * frame_dt - time.monotonic()
                    if sleep_for > 0:
                        time.sleep(sleep_for)
            finally:
                writer.release()
        except Exception as e:  # surfaced by start()/stop()
            self._error = e

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> Path:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        self._thread = None
        if self._error:
            raise RuntimeError(f"capture failed: {self._error}")
        return self.path


def pick_engine():
    ffmpeg = shutil.which("ffmpeg")
    return FfmpegRecorder(ffmpeg) if ffmpeg else PillowRecorder()


class MicMeter:
    """Live input level for the mic test: ffmpeg listens to the device and prints
    the peak level of every ~46 ms block; we parse those lines as they arrive."""

    SILENCE_DB = -60.0

    def __init__(self, ffmpeg: str, audio: AudioSettings) -> None:
        self.ffmpeg = ffmpeg
        self.audio = audio
        self.level_db = self.SILENCE_DB
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        cmd = [self.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info",
               "-f", "dshow", "-audio_buffer_size", str(AUDIO_BUFFER_MS), "-i", self.audio.device.dshow_input,
               "-af", f"{self.audio.filter},asetnsamples=n=2048,astats=metadata=1:reset=1,"
                      "ametadata=mode=print:key=lavfi.astats.Overall.Peak_level",
               "-f", "null", "-"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        threading.Thread(target=self._read, daemon=True, name="micmeter").start()

    def _read(self) -> None:
        proc = self.proc
        tail: list[str] = []
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            m = re.search(r"Peak_level=(-?[\d.]+|-inf)", line)
            if m:
                self.level_db = self.SILENCE_DB if m.group(1) == "-inf" else max(self.SILENCE_DB, float(m.group(1)))
            elif line and "Parsed_" not in line:
                tail = (tail + [line])[-3:]
        if proc.wait() not in (0, 255):   # 'q' gives 0/255; anything else means the device failed
            self.error = " / ".join(tail) or "microphone could not be opened"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    @property
    def level(self) -> float:
        """0.0 (silence) .. 1.0 (full scale)."""
        return max(0.0, min(1.0, (self.level_db - self.SILENCE_DB) / -self.SILENCE_DB))


# ---------------------------------------------------------------- settings --
def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- hotkey ----
def start_hotkey_listener(on_press) -> bool:
    """Register Ctrl+Shift+F9 system-wide.  Returns False if another app owns it."""
    ready = threading.Event()
    result = {"ok": False}

    def _loop():
        u = ctypes.windll.user32
        result["ok"] = bool(u.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_F9))
        ready.set()
        if not result["ok"]:
            return
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                on_press()

    threading.Thread(target=_loop, daemon=True, name="hotkey").start()
    ready.wait(timeout=2)
    return result["ok"]


# ---------------------------------------------------------------- GUI ------
# dark palette
BG, CARD, CARD_2, LINE = "#0e1117", "#161b25", "#1d2431", "#262e3d"
TEXT, MUTED, DIM = "#e9ecf3", "#8a93a8", "#5c6578"
RED, RED_HOVER, RED_DIM, GREEN, AMBER = "#e53935", "#c62828", "#7a2b2b", "#43a047", "#f9a825"
START_DELAY_MS = 450   # let the window finish minimising before the first frame
DETECTING = "Detecting…"


class RecorderApp:
    def __init__(self) -> None:
        import customtkinter as ctk

        self.ctk = ctk
        ctk.set_appearance_mode("dark")
        self.engine = pick_engine()
        self.ffmpeg = getattr(self.engine, "ffmpeg", None)
        self.targets = capture_targets()
        self.audio_devices: list[AudioDevice] = []
        self.events: queue.Queue[str] = queue.Queue()
        self.recording = False
        self.busy = False
        self.started_at = 0.0
        self.last_file: Path | None = None
        self.blink = False

        s = load_settings()
        self.root = ctk.CTk(fg_color=BG)
        self.root.title(APP_NAME)
        self.root.geometry("460x836")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.v_target = ctk.StringVar(value=self.targets[0][0])
        self.v_audio = ctk.StringVar(value=DETECTING if self.engine.supports_audio else "Needs ffmpeg")
        # older settings stored "No audio" as the device name; treat that as the tick being off
        self.v_audio_on = ctk.BooleanVar(value=self.engine.supports_audio
                                         and bool(s.get("audio_on", s.get("audio") != NO_AUDIO)))
        self.v_audio_quality = ctk.StringVar(value=s.get("audio_quality", "Standard"))
        self.v_gain = ctk.DoubleVar(value=max(0.0, min(float(MIC_GAIN_MAX), float(s.get("mic_gain", MIC_GAIN_DEFAULT)))))
        self.v_gain_text = ctk.StringVar(value="")
        self.v_auto_level = ctk.BooleanVar(value=bool(s.get("auto_level", True)))
        self.v_level = ctk.StringVar(value="")
        self.meter: MicMeter | None = None
        self.v_fps = ctk.StringVar(value=str(s.get("fps", 30)))
        self.v_quality = ctk.StringVar(value=s.get("quality", "Balanced"))
        self.v_out = ctk.StringVar(value=s.get("out_dir", str(DEFAULT_OUT_DIR)))
        self.v_minimize = ctk.BooleanVar(value=bool(s.get("minimize", True)))
        self.v_status = ctk.StringVar(value="Ready")
        self.v_summary = ctk.StringVar(value="")
        self.v_clock = ctk.StringVar(value="00:00:00")
        self.v_last = ctk.StringVar(value="No recording yet")
        self.wanted_audio = s.get("audio", "default")

        self._build()
        hot = start_hotkey_listener(lambda: self.events.put("toggle"))
        self.l_hotkey.configure(text=f"{HOTKEY_LABEL}  starts / stops" if hot
                                else f"{HOTKEY_LABEL} is taken by another app")
        for v in (self.v_target, self.v_audio, self.v_audio_on, self.v_audio_quality, self.v_gain, self.v_auto_level,
                  self.v_fps, self.v_quality):
            v.trace_add("write", lambda *_: self._refresh_summary())
        self._refresh_summary()
        if self.engine.supports_audio:
            threading.Thread(target=self._detect_audio, daemon=True).start()
        self.root.after(100, self._poll)

    # -- layout ---------------------------------------------------------
    def _font(self, size: int, weight: str = "normal", family: str = "Segoe UI"):
        return self.ctk.CTkFont(family=family, size=size, weight=weight)

    def _section(self, parent, text: str, row: int, column: int = 0, columnspan: int = 3, pady=(14, 4)):
        self.ctk.CTkLabel(parent, text=text.upper(), font=self._font(11, "bold"), text_color=MUTED,
                          anchor="w").grid(row=row, column=column, columnspan=columnspan, sticky="w", pady=pady)

    def _build(self) -> None:
        ctk = self.ctk
        f = ctk.CTkFrame(self.root, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=22, pady=(18, 16))
        f.columnconfigure(0, weight=1)

        # header
        ctk.CTkLabel(f, text="S B M   L A B S", font=self._font(11, "bold"), text_color=RED, anchor="w").grid(
            row=0, column=0, sticky="w")
        ctk.CTkLabel(f, text="Screen Recorder", font=self._font(24, "bold"), text_color=TEXT, anchor="w").grid(
            row=1, column=0, sticky="w", pady=(0, 12))

        # status card
        card = ctk.CTkFrame(f, fg_color=CARD, corner_radius=16, border_width=1, border_color=LINE)
        card.grid(row=2, column=0, sticky="we")
        card.columnconfigure(1, weight=1)
        self.l_dot = ctk.CTkLabel(card, text="●", font=self._font(22), text_color=DIM, width=24)
        self.l_dot.grid(row=0, column=0, padx=(16, 8), pady=(14, 0), sticky="w")
        ctk.CTkLabel(card, textvariable=self.v_status, font=self._font(17, "bold"), text_color=TEXT, anchor="w").grid(
            row=0, column=1, sticky="w", pady=(14, 0))
        ctk.CTkLabel(card, textvariable=self.v_clock, font=self._font(22, "bold", "Consolas"), text_color=TEXT).grid(
            row=0, column=2, padx=16, pady=(14, 0), sticky="e")
        ctk.CTkLabel(card, textvariable=self.v_summary, font=self._font(12), text_color=MUTED, anchor="w",
                     wraplength=380, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(2, 14))

        # settings
        body = ctk.CTkFrame(f, fg_color="transparent")
        body.grid(row=3, column=0, sticky="we")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        menu_kw = dict(height=36, corner_radius=10, fg_color=CARD_2, button_color=LINE, button_hover_color=DIM,
                       dropdown_fg_color=CARD_2, dropdown_hover_color=LINE, text_color=TEXT,
                       font=self._font(13), dropdown_font=self._font(13), dynamic_resizing=False, anchor="w")

        self._section(body, "Capture", 0, pady=(10, 4))
        self.m_target = ctk.CTkOptionMenu(body, values=[t[0] for t in self.targets], variable=self.v_target, **menu_kw)
        self.m_target.grid(row=1, column=0, columnspan=3, sticky="we")

        # the section label is itself the tick box: audio is optional
        self.k_audio = ctk.CTkCheckBox(body, text="RECORD AUDIO", variable=self.v_audio_on, command=self._audio_toggled,
                                       font=self._font(11, "bold"), text_color=MUTED, checkbox_width=18,
                                       checkbox_height=18, corner_radius=5, border_width=2, border_color=DIM,
                                       fg_color=RED, hover_color=RED_HOVER,
                                       state="normal" if self.engine.supports_audio else "disabled")
        self.k_audio.grid(row=2, column=0, sticky="w", pady=(14, 4))
        small_kw = dict(height=26, corner_radius=8, fg_color=CARD_2, hover_color=LINE, text_color=MUTED)
        self.b_test = ctk.CTkButton(body, text="Test mic", width=74, font=self._font(12), command=self.test_mic,
                                    state="disabled", **small_kw)
        self.b_test.grid(row=2, column=1, sticky="e", padx=(0, 6), pady=(14, 4))
        self.b_refresh = ctk.CTkButton(body, text="↻", width=32, font=self._font(14), command=self.refresh_audio,
                                       state="normal" if self.engine.supports_audio else "disabled", **small_kw)
        self.b_refresh.grid(row=2, column=2, sticky="e", pady=(14, 4))
        self.m_audio = ctk.CTkOptionMenu(body, values=[self.v_audio.get()], variable=self.v_audio,
                                         command=lambda _v: self._audio_changed(), **menu_kw)
        self.m_audio.grid(row=3, column=0, columnspan=3, sticky="we")

        seg_kw = dict(height=34, corner_radius=10, fg_color=CARD_2, unselected_color=CARD_2, unselected_hover_color=LINE,
                      selected_color=RED, selected_hover_color=RED_HOVER, text_color=TEXT, font=self._font(13))
        # level meter (mic test) on the left, audio quality preset on the right
        arow = ctk.CTkFrame(body, fg_color="transparent")
        arow.grid(row=4, column=0, columnspan=3, sticky="we", pady=(8, 0))
        arow.columnconfigure(0, weight=1)
        self.p_level = ctk.CTkProgressBar(arow, height=10, corner_radius=5, fg_color=CARD_2, progress_color=GREEN)
        self.p_level.set(0)
        self.p_level.grid(row=0, column=0, sticky="we")
        ctk.CTkLabel(arow, textvariable=self.v_level, font=self._font(11, family="Consolas"), text_color=MUTED,
                     width=64, anchor="e").grid(row=0, column=1, padx=(8, 10))
        self.s_audio_q = ctk.CTkSegmentedButton(arow, values=list(AUDIO_PRESETS), variable=self.v_audio_quality,
                                                width=150, **{**seg_kw, "height": 28, "font": self._font(12)})
        self.s_audio_q.grid(row=0, column=2, sticky="e")

        # mic boost slider + auto level switch
        grow = ctk.CTkFrame(body, fg_color="transparent")
        grow.grid(row=5, column=0, columnspan=3, sticky="we", pady=(10, 0))
        grow.columnconfigure(1, weight=1)
        ctk.CTkLabel(grow, text="MIC BOOST", font=self._font(11, "bold"), text_color=MUTED, anchor="w").grid(
            row=0, column=0, padx=(0, 10))
        self.sl_gain = ctk.CTkSlider(grow, from_=0, to=MIC_GAIN_MAX, number_of_steps=MIC_GAIN_MAX, variable=self.v_gain,
                                     height=16, fg_color=CARD_2, progress_color=RED, button_color=TEXT,
                                     button_hover_color="#ffffff", command=lambda _v: self._gain_text())
        self.sl_gain.grid(row=0, column=1, sticky="we")
        self.sl_gain.bind("<ButtonRelease-1>", lambda _e: self._audio_changed())
        ctk.CTkLabel(grow, textvariable=self.v_gain_text, font=self._font(11, family="Consolas"), text_color=TEXT,
                     width=52, anchor="e").grid(row=0, column=2, padx=(8, 10))
        self.sw_auto = ctk.CTkSwitch(grow, text="Auto level", variable=self.v_auto_level, progress_color=RED,
                                     font=self._font(12), text_color=TEXT, width=100, command=self._audio_changed)
        self.sw_auto.grid(row=0, column=3, sticky="e")
        self._gain_text()
        self._audio_toggled()

        self._section(body, "Frame rate", 6, columnspan=1)
        self._section(body, "Quality", 6, column=1, columnspan=2)
        self.s_fps = ctk.CTkSegmentedButton(body, values=[str(x) for x in FPS_CHOICES], variable=self.v_fps, **seg_kw)
        self.s_fps.grid(row=7, column=0, sticky="we", padx=(0, 8))
        self.s_quality = ctk.CTkSegmentedButton(body, values=list(QUALITY_PRESETS), variable=self.v_quality, **seg_kw)
        self.s_quality.grid(row=7, column=1, columnspan=2, sticky="we")

        self._section(body, "Save to", 8)
        self.e_out = ctk.CTkEntry(body, textvariable=self.v_out, height=36, corner_radius=10, fg_color=CARD_2,
                                  border_color=LINE, text_color=TEXT, font=self._font(13))
        self.e_out.grid(row=9, column=0, columnspan=2, sticky="we", padx=(0, 8))
        self.b_browse = ctk.CTkButton(body, text="Browse", width=84, height=36, corner_radius=10, fg_color=CARD_2,
                                      hover_color=LINE, text_color=TEXT, font=self._font(13), command=self.browse)
        self.b_browse.grid(row=9, column=2, sticky="e")

        self.sw_min = ctk.CTkSwitch(body, text="Minimise window while recording", variable=self.v_minimize,
                                    progress_color=RED, font=self._font(13), text_color=TEXT)
        self.sw_min.grid(row=10, column=0, columnspan=3, sticky="w", pady=(16, 0))

        # actions
        self.b_toggle = ctk.CTkButton(f, text="●   Start recording", height=50, corner_radius=14, fg_color=RED,
                                      hover_color=RED_HOVER, text_color="white", font=self._font(15, "bold"),
                                      command=self.toggle)
        self.b_toggle.grid(row=4, column=0, sticky="we", pady=(18, 8))
        row = ctk.CTkFrame(f, fg_color="transparent")
        row.grid(row=5, column=0, sticky="we")
        row.columnconfigure(2, weight=1)
        btn_kw = dict(height=34, corner_radius=10, fg_color=CARD_2, hover_color=LINE, text_color=TEXT, font=self._font(13))
        ctk.CTkButton(row, text="Open folder", width=110, command=self.open_folder, **btn_kw).grid(row=0, column=0)
        self.b_play = ctk.CTkButton(row, text="▶  Play last", width=110, command=self.play_last, state="disabled", **btn_kw)
        self.b_play.grid(row=0, column=1, padx=(8, 0))
        self.l_hotkey = ctk.CTkLabel(row, text="", font=self._font(12), text_color=MUTED, anchor="e",
                                     justify="right", wraplength=150)
        self.l_hotkey.grid(row=0, column=2, sticky="e")

        # footer
        ctk.CTkLabel(f, textvariable=self.v_last, font=self._font(12), text_color=MUTED, anchor="w",
                     wraplength=410, justify="left").grid(row=6, column=0, sticky="w", pady=(14, 0))
        ctk.CTkLabel(f, text=f"Engine  {self.engine.name}", font=self._font(11), text_color=DIM, anchor="w").grid(
            row=7, column=0, sticky="w", pady=(4, 0))

    # -- audio devices --------------------------------------------------
    def _detect_audio(self) -> None:
        self.audio_devices = list_audio_devices(self.ffmpeg)
        self.events.put("audio")

    def refresh_audio(self) -> None:
        if self.busy or self.recording or not self.engine.supports_audio:
            return
        self.wanted_audio = self.v_audio.get()
        self.v_audio.set(DETECTING)
        threading.Thread(target=self._detect_audio, daemon=True).start()

    def _apply_audio_list(self) -> None:
        if self.audio_devices:
            self.m_audio.configure(values=[d.name for d in self.audio_devices])
            chosen = find_audio_device(self.audio_devices, self.wanted_audio) or self.audio_devices[0]
            self.v_audio.set(chosen.name)
            self.k_audio.configure(state="normal")
        else:
            self.m_audio.configure(values=["No input devices found"])
            self.v_audio.set("No input devices found")
            self.v_audio_on.set(False)
            self.k_audio.configure(state="disabled")
        self._audio_toggled()

    def _audio_toggled(self) -> None:
        """Tick box drives the device controls: greyed out unless audio is wanted."""
        on = self.v_audio_on.get() and bool(self.audio_devices)
        state = "normal" if on else "disabled"
        for w in (self.m_audio, self.b_test, self.s_audio_q, self.sl_gain, self.sw_auto):
            w.configure(state=state)
        self.k_audio.configure(text_color=TEXT if self.v_audio_on.get() else MUTED)
        if not on:
            self._stop_meter()

    def _selected_audio(self) -> AudioDevice | None:
        if not self.v_audio_on.get():
            return None
        return find_audio_device(self.audio_devices, self.v_audio.get())

    # -- mic test -------------------------------------------------------
    def test_mic(self) -> None:
        if self.meter is not None:
            self._stop_meter()
            return
        audio = self._audio_settings()
        if audio is None or self.recording or self.busy:
            return
        self.meter = MicMeter(self.ffmpeg, audio)
        self.meter.start()
        self.b_test.configure(text="Stop test", text_color=TEXT)
        self.v_level.set("listening")

    def _stop_meter(self) -> None:
        if self.meter is None:
            return
        self.meter.stop()
        self.meter = None
        self.p_level.set(0)
        self.v_level.set("")
        self.b_test.configure(text="Test mic", text_color=MUTED)

    def _audio_changed(self) -> None:
        """Device, boost or auto-level changed: restart the live meter so it reflects the new chain."""
        if self.meter is not None:
            self._stop_meter()
            self.test_mic()

    def _gain_text(self) -> None:
        self.v_gain_text.set(f"+{self.v_gain.get():.0f} dB")

    def _audio_settings(self) -> AudioSettings | None:
        device = self._selected_audio()
        if device is None:
            return None
        return AudioSettings(device, self.v_audio_quality.get(), float(self.v_gain.get()), bool(self.v_auto_level.get()))

    def _tick_meter(self) -> None:
        m = self.meter
        if m is None:
            return
        if m.error or not m.alive():
            err = m.error or "microphone stopped"
            self._stop_meter()
            self.v_level.set("failed")
            self._error(f"Microphone test failed:\n{err}")
            return
        level, db = m.level, m.level_db
        self.p_level.set(level)
        self.p_level.configure(progress_color=RED if db > -3 else AMBER if db > -12 else GREEN)
        self.v_level.set(f"{db:5.1f} dB" if db > MicMeter.SILENCE_DB else "silent")

    # -- helpers --------------------------------------------------------
    def _refresh_summary(self) -> None:
        if self.v_audio_on.get():
            a = self._audio_settings()
            audio = self.v_audio.get() + (f"  ({a.label})" if a else "")
        else:
            audio = "no audio"
        self.v_summary.set(f"{self.v_target.get()}  ·  {self.v_fps.get()} fps  ·  {self.v_quality.get()}  ·  {audio}")

    def _set_inputs(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (self.m_target, self.s_fps, self.s_quality, self.e_out, self.b_browse, self.sw_min):
            w.configure(state=state)
        if self.engine.supports_audio:
            self.b_refresh.configure(state=state)
            if enabled:
                self.k_audio.configure(state="normal" if self.audio_devices else "disabled")
                self._audio_toggled()
            else:
                self._stop_meter()
                for w in (self.k_audio, self.m_audio, self.b_test, self.s_audio_q, self.sl_gain, self.sw_auto):
                    w.configure(state="disabled")

    def _error(self, text: str) -> None:
        from tkinter import messagebox
        messagebox.showerror(APP_NAME, text, parent=self.root)

    # -- actions --------------------------------------------------------
    def browse(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.v_out.get() or str(Path.home()), title="Save recordings to")
        if d:
            self.v_out.set(str(Path(d)))

    def open_folder(self) -> None:
        d = Path(self.v_out.get() or DEFAULT_OUT_DIR)
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)

    def play_last(self) -> None:
        if self.last_file and self.last_file.exists():
            os.startfile(self.last_file)

    def toggle(self) -> None:
        if self.busy:
            return
        self.stop() if self.recording else self.start()

    def start(self) -> None:
        if self.v_audio_on.get() and self.v_audio.get() == DETECTING:
            self._error("Still detecting audio devices, try again in a second (or untick Record audio).")
            return
        self.busy = True
        self._set_inputs(False)
        self.b_toggle.configure(state="disabled", text="Starting…")
        self.v_status.set("Starting…")
        delay = 0
        if self.v_minimize.get():
            self.root.iconify()
            delay = START_DELAY_MS   # so the minimise animation is not in the clip
        self.root.after(delay, self._start_engine)

    def _start_engine(self) -> None:
        out_dir = Path(self.v_out.get().strip() or DEFAULT_OUT_DIR)
        region = dict(self.targets)[self.v_target.get()]
        audio = self._audio_settings()
        try:
            path = new_output_path(out_dir)
            self.engine.start(path, int(self.v_fps.get()), self.v_quality.get(), region, audio)
        except Exception as e:
            self.busy = False
            self._set_inputs(True)
            self.b_toggle.configure(state="normal", text="●   Start recording")
            self.v_status.set("Ready")
            if self.root.state() == "iconic":
                self.root.deiconify()
            self._error(str(e))
            return
        save_settings({"fps": int(self.v_fps.get()), "quality": self.v_quality.get(), "out_dir": str(out_dir),
                       "minimize": bool(self.v_minimize.get()), "audio_on": bool(self.v_audio_on.get()),
                       "audio_quality": self.v_audio_quality.get(), "mic_gain": float(self.v_gain.get()),
                       "auto_level": bool(self.v_auto_level.get()),
                       # keep the device choice even when unticked, so re-ticking brings it back
                       "audio": self.v_audio.get() if self.audio_devices else "default"})
        self.busy = False
        self.recording = True
        self.started_at = time.monotonic()
        self.v_status.set("Recording")
        self.l_dot.configure(text_color=RED)
        self.v_last.set(f"Writing {path.name}")
        self.b_toggle.configure(state="normal", text="■   Stop recording", fg_color=CARD_2, hover_color=LINE)

    def stop(self) -> None:
        self.busy = True
        self.v_status.set("Finalising…")
        self.l_dot.configure(text_color=AMBER)
        self.b_toggle.configure(state="disabled")
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self) -> None:
        try:
            path = self.engine.stop()
            self.events.put(f"done:{path}")
        except Exception as e:
            self.events.put(f"error:{e}")

    def _on_stopped(self, msg: str) -> None:
        self.recording = False
        self.busy = False
        self.b_toggle.configure(state="normal", text="●   Start recording", fg_color=RED, hover_color=RED_HOVER)
        self._set_inputs(True)
        self.v_clock.set("00:00:00")
        if self.root.state() == "iconic":
            self.root.deiconify()
        if msg.startswith("done:"):
            self.last_file = Path(msg[5:])
            size = self.last_file.stat().st_size / 1e6 if self.last_file.exists() else 0
            self.v_status.set("Saved")
            self.l_dot.configure(text_color=GREEN)
            self.v_last.set(f"Saved {self.last_file.name}  ({size:.1f} MB)\n{self.last_file.parent}")
            self.b_play.configure(state="normal")
        else:
            self.v_status.set("Failed")
            self.l_dot.configure(text_color=AMBER)
            self.v_last.set(msg[6:])
            self._error(msg[6:])

    def _poll(self) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev == "toggle":
                self.toggle()
            elif ev == "audio":
                self._apply_audio_list()
            else:
                self._on_stopped(ev)
        self._tick_meter()
        self.ticks = getattr(self, "ticks", 0) + 1
        if self.recording:
            secs = int(time.monotonic() - self.started_at)
            self.v_clock.set(f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}")
            if not self.busy:
                if self.ticks % 5 == 0:   # blink the dot every 500 ms
                    self.blink = not self.blink
                    self.l_dot.configure(text_color=RED if self.blink else RED_DIM)
                if not self.engine.alive():
                    self.stop()  # encoder died -> surface its error
        self.root.after(100, self._poll)

    def on_close(self) -> None:
        self._stop_meter()
        if self.recording:
            try:
                self.engine.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_gui() -> int:
    try:
        import customtkinter  # noqa: F401
    except ImportError:
        import tkinter as tk
        from tkinter import messagebox
        tk.Tk().withdraw()
        messagebox.showerror(APP_NAME, "The GUI needs the customtkinter package.\n\n"
                                       "Run:   python -m pip install customtkinter\n\n"
                                       "(The CLI works without it:  python recorder.py --cli)")
        return 1
    RecorderApp().run()
    return 0


# ---------------------------------------------------------------- CLI ------
def run_cli(args: argparse.Namespace) -> int:
    engine = pick_engine()
    targets = capture_targets()
    if args.monitor < 0 or args.monitor >= len(targets):
        print(f"--monitor must be 0 (full desktop) .. {len(targets) - 1}", file=sys.stderr)
        for i, (label, _) in enumerate(targets):
            print(f"  {i}: {label}", file=sys.stderr)
        return 2
    label, region = targets[args.monitor]

    audio: AudioSettings | None = None
    if args.audio:
        if not engine.supports_audio:
            print("Audio needs ffmpeg on PATH (winget install Gyan.FFmpeg); recording video only.", file=sys.stderr)
        else:
            devices = list_audio_devices(engine.ffmpeg)
            device = find_audio_device(devices, args.audio)
            if device is None:
                print(f"No audio input matches '{args.audio}'.  Available:", file=sys.stderr)
                for d in devices:
                    print(f"  {d.name}", file=sys.stderr)
                return 2
            audio = AudioSettings(device, args.audio_quality, args.mic_gain, not args.no_auto_level)

    path = new_output_path(Path(args.out))
    print(f"{APP_NAME}  --  {engine.name}")
    print(f"Capturing {label} at {args.fps} fps, quality {args.quality}, "
          f"audio: {audio.device.name + ' (' + audio.label + ')' if audio else 'none'}")
    print(f"Writing   {path}")
    try:
        engine.start(path, args.fps, args.quality, region, audio)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    try:
        if args.duration:
            print(f"Recording for {args.duration:g} s  (Ctrl+C to stop early)")
            while time.monotonic() - t0 < args.duration and engine.alive():
                time.sleep(0.2)
        else:
            print("Recording.  Press Enter or Ctrl+C to stop.")
            input()
    except (KeyboardInterrupt, EOFError):
        pass
    print("Finalising...")
    try:
        out = engine.stop()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Saved {out}  ({out.stat().st_size / 1e6:.1f} MB, {time.monotonic() - t0:.1f} s)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument("--cli", action="store_true", help="record from the terminal instead of the GUI")
    p.add_argument("--list-audio", action="store_true", help="list audio input devices and exit")
    p.add_argument("--audio", metavar="NAME", help="audio input: 'default' (first device) or part of its name")
    p.add_argument("--audio-quality", default="Standard", choices=list(AUDIO_PRESETS),
                   help="Standard = AAC 160k/44.1kHz, High = AAC 320k/48kHz")
    p.add_argument("--mic-gain", type=float, default=MIC_GAIN_DEFAULT, metavar="DB",
                   help=f"mic boost in dB, 0-{MIC_GAIN_MAX} (default {MIC_GAIN_DEFAULT})")
    p.add_argument("--no-auto-level", action="store_true",
                   help="fixed gain instead of speech-adaptive levelling")
    p.add_argument("--fps", type=int, default=30, choices=FPS_CHOICES)
    p.add_argument("--quality", default="Balanced", choices=list(QUALITY_PRESETS))
    p.add_argument("--monitor", type=int, default=0, help="0 = full desktop, 1.. = that monitor")
    p.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="folder for the MP4 files")
    p.add_argument("--duration", type=float, default=0, help="seconds to record (CLI only; 0 = until stopped)")
    args = p.parse_args()

    _make_dpi_aware()
    if args.list_audio:
        devices = list_audio_devices(shutil.which("ffmpeg"))
        print("\n".join(d.name for d in devices) if devices else "No audio input devices found (is ffmpeg on PATH?)")
        return 0
    if args.cli:
        return run_cli(args)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
