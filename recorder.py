"""SBM Labs Screen Recorder -- desktop, webcam and microphone to MP4.

Any combination of three sources, each an optional tick:
  * Desktop  -- whole virtual screen or one monitor, with the mouse cursor
  * Camera   -- any DirectShow webcam; drawn top-right over the desktop as a
                small picture-in-picture, or full-frame when the desktop is off
  * Audio    -- any DirectShow input (mic, USB audio, Stereo Mix) with boost

    python recorder.py                                  # GUI (Ctrl+Shift+F9 starts / stops)
    python recorder.py --cli --audio default            # desktop + mic until Enter / Ctrl+C
    python recorder.py --cli --camera default           # desktop + webcam PiP
    python recorder.py --cli --no-desktop --camera default --audio default   # webcam only
    python recorder.py --list-devices

Engine: ffmpeg (gdigrab + dshow -> libx264 / aac) when it is on PATH.  Without
ffmpeg a Pillow + OpenCV fallback records the desktop only (no cursor, no audio,
no camera).  GUI needs `customtkinter`; the CLI does not.
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
# camera picture-in-picture width as a fraction of the desktop frame width
CAMERA_SIZES = {"Small": 0.16, "Medium": 0.22, "Large": 0.30}
PIP_MARGIN = 16         # px from the top-right corner
PIP_BORDER = 3          # px white frame around the camera picture

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


def new_output_path(out_dir: Path, ext: str = ".mp4") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"sbmlabs_rec_{datetime.now():%Y%m%d_%H%M%S}{ext}"


# ---------------------------------------------------------------- devices ---
@dataclass
class DshowDevice:
    name: str          # friendly name shown to the user
    kind: str          # "audio" or "video"
    alt: str = ""      # DirectShow "@device_..." id, stable even with duplicate names

    @property
    def dshow_input(self) -> str:
        return f"{self.kind}={self.alt or self.name}"


def list_dshow_devices(ffmpeg: str | None) -> tuple[list[DshowDevice], list[DshowDevice]]:
    """(audio inputs, cameras) as ffmpeg reports them."""
    if not ffmpeg:
        return [], []
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=NO_WINDOW,
        ).stderr
    except Exception:
        return [], []
    audio: list[DshowDevice] = []
    video: list[DshowDevice] = []
    current: DshowDevice | None = None
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
        if not m:
            continue
        # "(none)" is a video-category device with no usable pins (e.g. OBS Virtual
        # Camera while OBS is closed) -- it cannot be opened, so leave it out
        current = DshowDevice(m.group(1), kind) if kind in ("audio", "video") else None
        if current:
            (audio if kind == "audio" else video).append(current)
    return audio, video


def find_device(devices: list[DshowDevice], wanted: str | None) -> DshowDevice | None:
    """'default' -> first device; otherwise exact, then case-insensitive substring match on the name."""
    if not wanted or not devices:
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


@dataclass
class AudioSettings:
    """Everything the audio path needs: which device, how loud, how to encode."""
    device: DshowDevice
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


@dataclass
class CameraSettings:
    device: DshowDevice
    size: str = "Small"     # key of CAMERA_SIZES; only matters when drawn over the desktop

    @property
    def fraction(self) -> float:
        return CAMERA_SIZES.get(self.size, CAMERA_SIZES["Small"])


@dataclass
class RecordJob:
    path: Path
    fps: int = 30
    quality: str = "Balanced"
    desktop: bool = True
    region: Region | None = None       # None = whole virtual desktop
    camera: CameraSettings | None = None
    audio: AudioSettings | None = None

    @property
    def has_video(self) -> bool:
        return self.desktop or self.camera is not None

    @property
    def desktop_width(self) -> int:
        return (self.region or virtual_desktop())[2]

    def describe(self) -> str:
        parts = []
        if self.desktop:
            parts.append("desktop")
        if self.camera:
            parts.append(f"camera {self.camera.device.name}" + (f" ({self.camera.size} PiP)" if self.desktop else ""))
        if self.audio:
            parts.append(f"audio {self.audio.device.name} ({self.audio.label})")
        return " + ".join(parts) or "nothing"


# ---------------------------------------------------------------- engines ---
class FfmpegRecorder:
    """gdigrab desktop + dshow camera / mic -> libx264 / aac.  Stopped by sending 'q'
    so the MP4 is finalised."""

    name = "ffmpeg (gdigrab + dshow -> libx264 / aac)"
    supports_devices = True

    def __init__(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg
        self.proc: subprocess.Popen | None = None
        self.path: Path | None = None

    def build_command(self, job: RecordJob) -> list[str]:
        crf, preset = QUALITY_PRESETS[job.quality]
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        idx: dict[str, int] = {}          # input kind -> ffmpeg input index

        def add_input(kind: str, args: list[str]) -> None:
            idx[kind] = len(idx)
            cmd.extend(args)

        if job.audio:
            add_input("a", ["-f", "dshow", "-audio_buffer_size", str(AUDIO_BUFFER_MS),
                            "-thread_queue_size", "1024", "-rtbufsize", "64M", "-i", job.audio.device.dshow_input])
        if job.desktop:
            args = ["-f", "gdigrab", "-framerate", str(job.fps), "-draw_mouse", "1",
                    "-rtbufsize", "256M", "-thread_queue_size", "512"]
            if job.region is not None:
                x, y, w, h = job.region
                args += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
            add_input("d", args + ["-i", "desktop"])
        if job.camera:
            add_input("c", ["-f", "dshow", "-rtbufsize", "100M", "-thread_queue_size", "512",
                            "-i", job.camera.device.dshow_input])

        even = "crop=trunc(iw/2)*2:trunc(ih/2)*2"   # yuv420p needs even dimensions
        if "d" in idx and "c" in idx:
            # camera scaled to a fraction of the desktop width, white frame, top-right corner
            cam_w = max(2, round(job.desktop_width * job.camera.fraction / 2) * 2)
            b, m = PIP_BORDER, PIP_MARGIN
            graph = (f"[{idx['d']}:v]{even}[desk];"
                     f"[{idx['c']}:v]scale={cam_w}:-2,pad=iw+{2 * b}:ih+{2 * b}:{b}:{b}:color=white[cam];"
                     f"[desk][cam]overlay=x=W-w-{m}:y={m}:eof_action=pass[v]")
            cmd += ["-filter_complex", graph, "-map", "[v]"]
        elif "d" in idx:
            cmd += ["-map", f"{idx['d']}:v:0", "-vf", even]
        elif "c" in idx:
            cmd += ["-map", f"{idx['c']}:v:0", "-vf", even, "-r", str(job.fps)]
        if job.audio:
            bitrate, sample_rate = AUDIO_PRESETS[job.audio.quality]
            cmd += ["-map", f"{idx['a']}:a:0", "-c:a", "aac", "-b:a", bitrate, "-ar", str(sample_rate),
                    "-af", f"aresample=async=1,{job.audio.filter}"]
        if job.has_video:
            cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
        cmd += ["-movflags", "+faststart", str(job.path)]
        return cmd

    def start(self, job: RecordJob) -> None:
        if not job.has_video and job.audio is None:
            raise RuntimeError("Nothing to record: tick at least one of desktop, camera or audio.")
        self.path = job.path
        self.proc = subprocess.Popen(
            self.build_command(job), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        # ffmpeg exits within a second or two if the args / devices are wrong; catch that early
        time.sleep(1.5 if job.camera else 1.0)
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
    """Fallback: ImageGrab frames written by OpenCV at a real-time paced fps.  Desktop only."""

    name = "Pillow ImageGrab + OpenCV (desktop only, no cursor)"
    supports_devices = False

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: Exception | None = None
        self.path: Path | None = None

    def start(self, job: RecordJob) -> None:
        import cv2  # noqa: F401  -- fail here, not inside the thread
        from PIL import ImageGrab  # noqa: F401

        if not job.desktop:
            raise RuntimeError("Camera and audio need ffmpeg on PATH (winget install Gyan.FFmpeg).")
        self.path = job.path
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, args=(job.path, job.fps, job.region), daemon=True)
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
    """Live input level for the mic test: ffmpeg listens to the device (through the
    same boost chain as a recording) and prints the peak level of every ~46 ms
    block; we parse those lines as they arrive."""

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


class CameraPreview:
    """A small always-on-top ffplay window showing the chosen camera.  DirectShow
    cameras cannot be opened twice, so this is closed before a recording starts."""

    def __init__(self, ffmpeg: str, device: DshowDevice) -> None:
        self.ffplay = shutil.which("ffplay") or str(Path(ffmpeg).with_name("ffplay.exe"))
        self.device = device
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if not Path(self.ffplay).exists():
            raise RuntimeError("ffplay.exe (ships with ffmpeg) was not found next to ffmpeg.")
        self.proc = subprocess.Popen(
            [self.ffplay, "-hide_banner", "-loglevel", "error", "-window_title", f"{APP_NAME} - camera preview",
             "-alwaysontop", "-x", "480", "-f", "dshow", "-i", self.device.dshow_input],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
        )

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def error(self) -> str:
        if self.proc is None or self.proc.poll() is None:
            return ""
        return self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


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
NONE_FOUND = "None found"


class RecorderApp:
    def __init__(self) -> None:
        import customtkinter as ctk

        self.ctk = ctk
        ctk.set_appearance_mode("dark")
        self.engine = pick_engine()
        self.ffmpeg = getattr(self.engine, "ffmpeg", None)
        self.devices_ok = self.engine.supports_devices
        self.targets = capture_targets()
        self.audio_devices: list[DshowDevice] = []
        self.cameras: list[DshowDevice] = []
        self.events: queue.Queue[str] = queue.Queue()
        self.recording = False
        self.busy = False
        self.started_at = 0.0
        self.last_file: Path | None = None
        self.blink = False
        self.ticks = 0
        self.meter: MicMeter | None = None
        self.preview: CameraPreview | None = None

        s = load_settings()
        self.root = ctk.CTk(fg_color=BG)
        self.root.title(APP_NAME)
        self.root.geometry("820x640")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        unavailable = DETECTING if self.devices_ok else "Needs ffmpeg"
        self.v_desktop_on = ctk.BooleanVar(value=bool(s.get("desktop_on", True)))
        self.v_target = ctk.StringVar(value=s.get("target", self.targets[0][0]))
        if self.v_target.get() not in dict(self.targets):
            self.v_target.set(self.targets[0][0])
        self.v_camera_on = ctk.BooleanVar(value=self.devices_ok and bool(s.get("camera_on", False)))
        self.v_camera = ctk.StringVar(value=unavailable)
        self.v_camera_size = ctk.StringVar(value=s.get("camera_size", "Small"))
        self.wanted_camera = s.get("camera", "default")
        # older settings stored "No audio" as the device name; treat that as the tick being off
        self.v_audio_on = ctk.BooleanVar(value=self.devices_ok and bool(s.get("audio_on", s.get("audio") != "No audio")))
        self.v_audio = ctk.StringVar(value=unavailable)
        self.v_audio_quality = ctk.StringVar(value=s.get("audio_quality", "Standard"))
        self.v_gain = ctk.DoubleVar(value=max(0.0, min(float(MIC_GAIN_MAX), float(s.get("mic_gain", MIC_GAIN_DEFAULT)))))
        self.v_gain_text = ctk.StringVar(value="")
        self.v_auto_level = ctk.BooleanVar(value=bool(s.get("auto_level", True)))
        self.v_level = ctk.StringVar(value="")
        self.wanted_audio = s.get("audio", "default")
        self.v_fps = ctk.StringVar(value=str(s.get("fps", 30)))
        self.v_quality = ctk.StringVar(value=s.get("quality", "Balanced"))
        self.v_out = ctk.StringVar(value=s.get("out_dir", str(DEFAULT_OUT_DIR)))
        self.v_minimize = ctk.BooleanVar(value=bool(s.get("minimize", True)))
        self.v_status = ctk.StringVar(value="Ready")
        self.v_summary = ctk.StringVar(value="")
        self.v_clock = ctk.StringVar(value="00:00:00")
        self.v_last = ctk.StringVar(value="No recording yet")

        self._build()
        hot = start_hotkey_listener(lambda: self.events.put("toggle"))
        self.l_hotkey.configure(text=f"Hotkey  {HOTKEY_LABEL}" if hot else "Hotkey taken by another app")
        for v in (self.v_desktop_on, self.v_target, self.v_camera_on, self.v_camera, self.v_camera_size,
                  self.v_audio_on, self.v_audio, self.v_audio_quality, self.v_gain, self.v_auto_level,
                  self.v_fps, self.v_quality):
            v.trace_add("write", lambda *_: self._refresh_summary())
        self._refresh_summary()
        if self.devices_ok:
            threading.Thread(target=self._detect_devices, daemon=True).start()
        self.root.after(100, self._poll)

    # -- layout ---------------------------------------------------------
    def _font(self, size: int, weight: str = "normal", family: str = "Segoe UI"):
        return self.ctk.CTkFont(family=family, size=size, weight=weight)

    def _label(self, parent, text: str, **grid):
        self.ctk.CTkLabel(parent, text=text.upper(), font=self._font(11, "bold"), text_color=MUTED,
                          anchor="w").grid(sticky="w", **grid)

    def _tick(self, parent, text: str, var, command):
        return self.ctk.CTkCheckBox(parent, text=text.upper(), variable=var, command=command,
                                    font=self._font(11, "bold"), text_color=MUTED, checkbox_width=18,
                                    checkbox_height=18, corner_radius=5, border_width=2, border_color=DIM,
                                    fg_color=RED, hover_color=RED_HOVER)

    def _card(self, parent, title: str, **grid):
        ctk = self.ctk
        outer = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1, border_color=LINE)
        outer.grid(**grid)
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=(12, 16))
        inner.columnconfigure(0, weight=1)
        ctk.CTkLabel(inner, text=title.upper(), font=self._font(10, "bold"), text_color=DIM, anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
        return inner

    def _build(self) -> None:
        ctk = self.ctk
        self.menu_kw = dict(height=34, corner_radius=10, fg_color=CARD_2, button_color=LINE, button_hover_color=DIM,
                            dropdown_fg_color=CARD_2, dropdown_hover_color=LINE, text_color=TEXT,
                            font=self._font(13), dropdown_font=self._font(13), dynamic_resizing=False, anchor="w")
        self.seg_kw = dict(height=32, corner_radius=10, fg_color=CARD_2, unselected_color=CARD_2,
                           unselected_hover_color=LINE, selected_color=RED, selected_hover_color=RED_HOVER,
                           text_color=TEXT, font=self._font(12))
        self.small_kw = dict(height=26, corner_radius=8, fg_color=CARD_2, hover_color=LINE, text_color=MUTED,
                             font=self._font(12))

        f = ctk.CTkFrame(self.root, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=20, pady=(14, 14))
        f.columnconfigure(0, weight=1, uniform="col")
        f.columnconfigure(1, weight=1, uniform="col")

        # header
        ctk.CTkLabel(f, text="S B M   L A B S", font=self._font(11, "bold"), text_color=RED, anchor="w").grid(
            row=0, column=0, sticky="w")
        ctk.CTkLabel(f, text="Screen Recorder", font=self._font(24, "bold"), text_color=TEXT, anchor="w").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # status card across both columns
        card = ctk.CTkFrame(f, fg_color=CARD, corner_radius=16, border_width=1, border_color=LINE)
        card.grid(row=2, column=0, columnspan=2, sticky="we", pady=(0, 12))
        card.columnconfigure(1, weight=1)
        self.l_dot = ctk.CTkLabel(card, text="●", font=self._font(22), text_color=DIM, width=24)
        self.l_dot.grid(row=0, column=0, padx=(16, 8), pady=(12, 0), sticky="w")
        ctk.CTkLabel(card, textvariable=self.v_status, font=self._font(17, "bold"), text_color=TEXT, anchor="w").grid(
            row=0, column=1, sticky="w", pady=(12, 0))
        ctk.CTkLabel(card, textvariable=self.v_clock, font=self._font(22, "bold", "Consolas"), text_color=TEXT).grid(
            row=0, column=2, padx=16, pady=(12, 0), sticky="e")
        ctk.CTkLabel(card, textvariable=self.v_summary, font=self._font(12), text_color=MUTED, anchor="w",
                     wraplength=740, justify="left").grid(row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(2, 12))

        self._build_sources(self._card(f, "Sources", row=3, column=0, sticky="nsew", padx=(0, 6)))
        self._build_output(self._card(f, "Output", row=3, column=1, sticky="nsew", padx=(6, 0)))

    def _build_sources(self, p) -> None:
        ctk = self.ctk
        p.columnconfigure(1, weight=1)
        dev_state = "normal" if self.devices_ok else "disabled"

        # desktop
        self.k_desktop = self._tick(p, "Record desktop", self.v_desktop_on, self._sources_toggled)
        self.k_desktop.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 4))
        self.m_target = ctk.CTkOptionMenu(p, values=[t[0] for t in self.targets], variable=self.v_target, **self.menu_kw)
        self.m_target.grid(row=2, column=0, columnspan=3, sticky="we")

        # camera
        self.k_camera = self._tick(p, "Record camera", self.v_camera_on, self._sources_toggled)
        self.k_camera.configure(state=dev_state)
        self.k_camera.grid(row=3, column=0, sticky="w", pady=(12, 4))
        self.s_cam_size = ctk.CTkSegmentedButton(p, values=list(CAMERA_SIZES), variable=self.v_camera_size,
                                                 width=150, **{**self.seg_kw, "height": 26, "font": self._font(11)})
        self.s_cam_size.grid(row=3, column=1, sticky="e", padx=(10, 6), pady=(12, 4))
        self.b_preview = ctk.CTkButton(p, text="Preview", width=64, command=self.toggle_preview, **self.small_kw)
        self.b_preview.grid(row=3, column=2, sticky="e", pady=(12, 4))
        self.m_camera = ctk.CTkOptionMenu(p, values=[self.v_camera.get()], variable=self.v_camera,
                                          command=lambda _v: self._camera_changed(), **self.menu_kw)
        self.m_camera.grid(row=4, column=0, columnspan=3, sticky="we")

        # audio
        self.k_audio = self._tick(p, "Record audio", self.v_audio_on, self._sources_toggled)
        self.k_audio.configure(state=dev_state)
        self.k_audio.grid(row=5, column=0, sticky="w", pady=(12, 4))
        self.b_test = ctk.CTkButton(p, text="Test mic", width=70, command=self.test_mic, **self.small_kw)
        self.b_test.grid(row=5, column=1, sticky="e", padx=(0, 6), pady=(12, 4))
        self.b_refresh = ctk.CTkButton(p, text="↻", width=30, command=self.refresh_devices, state=dev_state,
                                       **{**self.small_kw, "font": self._font(14)})
        self.b_refresh.grid(row=5, column=2, sticky="e", pady=(12, 4))
        self.m_audio = ctk.CTkOptionMenu(p, values=[self.v_audio.get()], variable=self.v_audio,
                                         command=lambda _v: self._audio_changed(), **self.menu_kw)
        self.m_audio.grid(row=6, column=0, columnspan=3, sticky="we")

        arow = ctk.CTkFrame(p, fg_color="transparent")
        arow.grid(row=7, column=0, columnspan=3, sticky="we", pady=(8, 0))
        arow.columnconfigure(0, weight=1)
        self.p_level = ctk.CTkProgressBar(arow, height=10, corner_radius=5, fg_color=CARD_2, progress_color=GREEN)
        self.p_level.set(0)
        self.p_level.grid(row=0, column=0, sticky="we")
        ctk.CTkLabel(arow, textvariable=self.v_level, font=self._font(11, family="Consolas"), text_color=MUTED,
                     width=60, anchor="e").grid(row=0, column=1, padx=(8, 8))
        self.s_audio_q = ctk.CTkSegmentedButton(arow, values=list(AUDIO_PRESETS), variable=self.v_audio_quality,
                                                width=130, **{**self.seg_kw, "height": 26, "font": self._font(11)})
        self.s_audio_q.grid(row=0, column=2, sticky="e")

        grow = ctk.CTkFrame(p, fg_color="transparent")
        grow.grid(row=8, column=0, columnspan=3, sticky="we", pady=(8, 0))
        grow.columnconfigure(1, weight=1)
        ctk.CTkLabel(grow, text="MIC BOOST", font=self._font(11, "bold"), text_color=MUTED, anchor="w").grid(
            row=0, column=0, padx=(0, 8))
        self.sl_gain = ctk.CTkSlider(grow, from_=0, to=MIC_GAIN_MAX, number_of_steps=MIC_GAIN_MAX, variable=self.v_gain,
                                     height=16, fg_color=CARD_2, progress_color=RED, button_color=TEXT,
                                     button_hover_color="#ffffff", command=lambda _v: self._gain_text())
        self.sl_gain.grid(row=0, column=1, sticky="we")
        self.sl_gain.bind("<ButtonRelease-1>", lambda _e: self._audio_changed())
        ctk.CTkLabel(grow, textvariable=self.v_gain_text, font=self._font(11, family="Consolas"), text_color=TEXT,
                     width=50, anchor="e").grid(row=0, column=2, padx=(6, 8))
        self.sw_auto = ctk.CTkSwitch(grow, text="Auto level", variable=self.v_auto_level, progress_color=RED,
                                     font=self._font(12), text_color=TEXT, width=96, command=self._audio_changed)
        self.sw_auto.grid(row=0, column=3, sticky="e")
        self._gain_text()
        self._sources_toggled()

    def _build_output(self, p) -> None:
        ctk = self.ctk
        p.columnconfigure(0, weight=1)
        p.columnconfigure(1, weight=1)

        self._label(p, "Frame rate", row=1, column=0, pady=(8, 4))
        self._label(p, "Quality", row=1, column=1, pady=(8, 4), padx=(8, 0))
        self.s_fps = ctk.CTkSegmentedButton(p, values=[str(x) for x in FPS_CHOICES], variable=self.v_fps, **self.seg_kw)
        self.s_fps.grid(row=2, column=0, sticky="we")
        self.s_quality = ctk.CTkSegmentedButton(p, values=list(QUALITY_PRESETS), variable=self.v_quality, **self.seg_kw)
        self.s_quality.grid(row=2, column=1, sticky="we", padx=(8, 0))

        self._label(p, "Save to", row=3, column=0, columnspan=2, pady=(12, 4))
        srow = ctk.CTkFrame(p, fg_color="transparent")
        srow.grid(row=4, column=0, columnspan=2, sticky="we")
        srow.columnconfigure(0, weight=1)
        self.e_out = ctk.CTkEntry(srow, textvariable=self.v_out, height=34, corner_radius=10, fg_color=CARD_2,
                                  border_color=LINE, text_color=TEXT, font=self._font(13))
        self.e_out.grid(row=0, column=0, sticky="we", padx=(0, 8))
        self.b_browse = ctk.CTkButton(srow, text="Browse", width=80, height=34, corner_radius=10, fg_color=CARD_2,
                                      hover_color=LINE, text_color=TEXT, font=self._font(13), command=self.browse)
        self.b_browse.grid(row=0, column=1)

        self.sw_min = ctk.CTkSwitch(p, text="Minimise window while recording", variable=self.v_minimize,
                                    progress_color=RED, font=self._font(13), text_color=TEXT)
        self.sw_min.grid(row=5, column=0, columnspan=2, sticky="w", pady=(14, 0))

        self.b_toggle = ctk.CTkButton(p, text="●   Start recording", height=48, corner_radius=14, fg_color=RED,
                                      hover_color=RED_HOVER, text_color="white", font=self._font(15, "bold"),
                                      command=self.toggle)
        self.b_toggle.grid(row=6, column=0, columnspan=2, sticky="we", pady=(18, 8))
        brow = ctk.CTkFrame(p, fg_color="transparent")
        brow.grid(row=7, column=0, columnspan=2, sticky="we")
        brow.columnconfigure(2, weight=1)
        btn_kw = dict(height=32, corner_radius=10, fg_color=CARD_2, hover_color=LINE, text_color=TEXT, font=self._font(12))
        ctk.CTkButton(brow, text="Open folder", width=100, command=self.open_folder, **btn_kw).grid(row=0, column=0)
        self.b_play = ctk.CTkButton(brow, text="▶  Play last", width=100, command=self.play_last, state="disabled", **btn_kw)
        self.b_play.grid(row=0, column=1, padx=(8, 0))
        self.l_hotkey = ctk.CTkLabel(brow, text="", font=self._font(11), text_color=MUTED, anchor="e",
                                     justify="right", wraplength=130)
        self.l_hotkey.grid(row=0, column=2, sticky="e")

        ctk.CTkLabel(p, textvariable=self.v_last, font=self._font(12), text_color=MUTED, anchor="w",
                     wraplength=330, justify="left").grid(row=8, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ctk.CTkLabel(p, text=f"Engine  {self.engine.name}", font=self._font(10), text_color=DIM, anchor="w",
                     wraplength=330, justify="left").grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # -- devices --------------------------------------------------------
    def _detect_devices(self) -> None:
        self.audio_devices, self.cameras = list_dshow_devices(self.ffmpeg)
        self.events.put("devices")

    def refresh_devices(self) -> None:
        if self.busy or self.recording or not self.devices_ok:
            return
        self.wanted_audio = self.v_audio.get()
        self.wanted_camera = self.v_camera.get()
        self.v_audio.set(DETECTING)
        self.v_camera.set(DETECTING)
        threading.Thread(target=self._detect_devices, daemon=True).start()

    def _apply_device_lists(self) -> None:
        for devices, menu, var, wanted, tick_var in (
            (self.audio_devices, self.m_audio, self.v_audio, self.wanted_audio, self.v_audio_on),
            (self.cameras, self.m_camera, self.v_camera, self.wanted_camera, self.v_camera_on),
        ):
            if devices:
                menu.configure(values=[d.name for d in devices])
                var.set((find_device(devices, wanted) or devices[0]).name)
            else:
                menu.configure(values=[NONE_FOUND])
                var.set(NONE_FOUND)
                tick_var.set(False)
        self._sources_toggled()

    def _selected_audio(self) -> AudioSettings | None:
        device = find_device(self.audio_devices, self.v_audio.get()) if self.v_audio_on.get() else None
        if device is None:
            return None
        return AudioSettings(device, self.v_audio_quality.get(), float(self.v_gain.get()), bool(self.v_auto_level.get()))

    def _selected_camera(self) -> CameraSettings | None:
        device = find_device(self.cameras, self.v_camera.get()) if self.v_camera_on.get() else None
        return CameraSettings(device, self.v_camera_size.get()) if device else None

    def _sources_toggled(self) -> None:
        """Each tick box drives its own controls: greyed out unless that source is wanted."""
        self.m_target.configure(state="normal" if self.v_desktop_on.get() else "disabled")
        self.k_desktop.configure(text_color=TEXT if self.v_desktop_on.get() else MUTED)

        cam = self.v_camera_on.get() and bool(self.cameras)
        for w in (self.m_camera, self.s_cam_size, self.b_preview):
            w.configure(state="normal" if cam else "disabled")
        self.k_camera.configure(state="normal" if self.cameras else "disabled",
                                text_color=TEXT if self.v_camera_on.get() else MUTED)
        # the PiP size only matters when there is a desktop to draw it on
        if cam and not self.v_desktop_on.get():
            self.s_cam_size.configure(state="disabled")
        if not cam:
            self._stop_preview()

        aud = self.v_audio_on.get() and bool(self.audio_devices)
        for w in (self.m_audio, self.b_test, self.s_audio_q, self.sl_gain, self.sw_auto):
            w.configure(state="normal" if aud else "disabled")
        self.k_audio.configure(state="normal" if self.audio_devices else "disabled",
                               text_color=TEXT if self.v_audio_on.get() else MUTED)
        if not aud:
            self._stop_meter()

    # -- mic test -------------------------------------------------------
    def test_mic(self) -> None:
        if self.meter is not None:
            self._stop_meter()
            return
        audio = self._selected_audio()
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

    # -- camera preview -------------------------------------------------
    def toggle_preview(self) -> None:
        if self.preview is not None:
            self._stop_preview()
            return
        cam = self._selected_camera()
        if cam is None or self.recording or self.busy:
            return
        self.preview = CameraPreview(self.ffmpeg, cam.device)
        try:
            self.preview.start()
        except Exception as e:
            self.preview = None
            self._error(str(e))
            return
        self.b_preview.configure(text="Close", text_color=TEXT)

    def _stop_preview(self) -> None:
        if self.preview is None:
            return
        self.preview.stop()
        self.preview = None
        self.b_preview.configure(text="Preview", text_color=MUTED)

    def _camera_changed(self) -> None:
        if self.preview is not None:
            self._stop_preview()
            self.toggle_preview()

    def _tick_preview(self) -> None:
        p = self.preview
        if p is not None and not p.alive():      # user closed the window, or the camera failed
            err = p.error()
            self._stop_preview()
            if err:
                self._error(f"Camera preview failed:\n{err}")

    # -- helpers --------------------------------------------------------
    def _job(self, out_dir: Path) -> RecordJob:
        audio = self._selected_audio()
        camera = self._selected_camera()
        desktop = bool(self.v_desktop_on.get())
        ext = ".mp4" if (desktop or camera) else ".m4a"
        return RecordJob(new_output_path(out_dir, ext), int(self.v_fps.get()), self.v_quality.get(), desktop,
                         dict(self.targets)[self.v_target.get()], camera, audio)

    def _refresh_summary(self) -> None:
        parts = []
        if self.v_desktop_on.get():
            parts.append(self.v_target.get())
        if self.v_camera_on.get():
            parts.append(f"camera {self.v_camera.get()}" + (f" ({self.v_camera_size.get()} PiP)" if self.v_desktop_on.get() else ""))
        if self.v_audio_on.get():
            a = self._selected_audio()
            parts.append(self.v_audio.get() + (f" ({a.label})" if a else ""))
        parts.append(f"{self.v_fps.get()} fps  ·  {self.v_quality.get()}")
        self.v_summary.set("  ·  ".join(parts) if len(parts) > 1 else "Nothing selected -- tick a source")

    def _set_inputs(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (self.s_fps, self.s_quality, self.e_out, self.b_browse, self.sw_min, self.b_refresh, self.k_desktop):
            w.configure(state=state)
        if enabled:
            self._sources_toggled()
        else:
            self._stop_meter()
            self._stop_preview()
            for w in (self.m_target, self.k_camera, self.m_camera, self.s_cam_size, self.b_preview,
                      self.k_audio, self.m_audio, self.b_test, self.s_audio_q, self.sl_gain, self.sw_auto):
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
        if not (self.v_desktop_on.get() or self.v_camera_on.get() or self.v_audio_on.get()):
            self._error("Nothing selected to record.\n\nTick at least one of desktop, camera or audio.")
            return
        if DETECTING in (self.v_audio.get(), self.v_camera.get()) and (self.v_audio_on.get() or self.v_camera_on.get()):
            self._error("Still detecting devices, try again in a second.")
            return
        self.busy = True
        self._set_inputs(False)     # also closes the mic test and camera preview (the camera can't be shared)
        self.b_toggle.configure(state="disabled", text="Starting…")
        self.v_status.set("Starting…")
        delay = 0
        if self.v_minimize.get() and self.v_desktop_on.get():
            self.root.iconify()
            delay = START_DELAY_MS   # so the minimise animation is not in the clip
        self.root.after(delay, self._start_engine)

    def _start_engine(self) -> None:
        out_dir = Path(self.v_out.get().strip() or DEFAULT_OUT_DIR)
        try:
            job = self._job(out_dir)
            self.engine.start(job)
        except Exception as e:
            self.busy = False
            self._set_inputs(True)
            self.b_toggle.configure(state="normal", text="●   Start recording")
            self.v_status.set("Ready")
            if self.root.state() == "iconic":
                self.root.deiconify()
            self._error(str(e))
            return
        save_settings({
            "desktop_on": bool(self.v_desktop_on.get()), "target": self.v_target.get(),
            "camera_on": bool(self.v_camera_on.get()), "camera_size": self.v_camera_size.get(),
            "camera": self.v_camera.get() if self.cameras else "default",
            "audio_on": bool(self.v_audio_on.get()), "audio_quality": self.v_audio_quality.get(),
            "mic_gain": float(self.v_gain.get()), "auto_level": bool(self.v_auto_level.get()),
            # keep the device choices even when unticked, so re-ticking brings them back
            "audio": self.v_audio.get() if self.audio_devices else "default",
            "fps": int(self.v_fps.get()), "quality": self.v_quality.get(), "out_dir": str(out_dir),
            "minimize": bool(self.v_minimize.get()),
        })
        self.busy = False
        self.recording = True
        self.started_at = time.monotonic()
        self.v_status.set("Recording")
        self.l_dot.configure(text_color=RED)
        self.v_last.set(f"Writing {job.path.name}")
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
            elif ev == "devices":
                self._apply_device_lists()
            else:
                self._on_stopped(ev)
        self._tick_meter()
        self._tick_preview()
        self.ticks += 1
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
        self._stop_preview()
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
    camera: CameraSettings | None = None
    if args.audio or args.camera:
        if not engine.supports_devices:
            print("Camera and audio need ffmpeg on PATH (winget install Gyan.FFmpeg); recording the desktop only.",
                  file=sys.stderr)
        else:
            audio_devices, cameras = list_dshow_devices(engine.ffmpeg)
            for wanted, devices, what in ((args.audio, audio_devices, "audio input"), (args.camera, cameras, "camera")):
                if wanted and find_device(devices, wanted) is None:
                    print(f"No {what} matches '{wanted}'.  Available:", file=sys.stderr)
                    for d in devices:
                        print(f"  {d.name}", file=sys.stderr)
                    return 2
            if args.audio:
                audio = AudioSettings(find_device(audio_devices, args.audio), args.audio_quality, args.mic_gain,
                                      not args.no_auto_level)
            if args.camera:
                camera = CameraSettings(find_device(cameras, args.camera), args.camera_size)

    desktop = not args.no_desktop
    ext = ".mp4" if (desktop or camera) else ".m4a"
    job = RecordJob(new_output_path(Path(args.out), ext), args.fps, args.quality, desktop, region, camera, audio)
    print(f"{APP_NAME}  --  {engine.name}")
    print(f"Recording {job.describe()}" + (f" [{label}]" if desktop else "") + f" at {args.fps} fps, quality {args.quality}")
    print(f"Writing   {job.path}")
    try:
        engine.start(job)
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
    p.add_argument("--list-devices", "--list-audio", action="store_true", help="list cameras and audio inputs, then exit")
    p.add_argument("--no-desktop", action="store_true", help="leave the desktop out (camera and/or audio only)")
    p.add_argument("--camera", metavar="NAME", help="webcam: 'default' (first camera) or part of its name")
    p.add_argument("--camera-size", default="Small", choices=list(CAMERA_SIZES),
                   help="picture-in-picture size when drawn over the desktop")
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
    p.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="folder for the recordings")
    p.add_argument("--duration", type=float, default=0, help="seconds to record (CLI only; 0 = until stopped)")
    args = p.parse_args()

    _make_dpi_aware()
    if args.list_devices:
        audio, cameras = list_dshow_devices(shutil.which("ffmpeg"))
        print("Cameras:\n" + ("\n".join(f"  {d.name}" for d in cameras) or "  (none)"))
        print("Audio inputs:\n" + ("\n".join(f"  {d.name}" for d in audio) or "  (none -- is ffmpeg on PATH?)"))
        return 0
    if args.cli:
        return run_cli(args)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
