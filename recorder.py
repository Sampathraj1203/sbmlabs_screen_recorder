"""SBM Labs Screen Recorder -- records the Windows desktop to an MP4 file.

Desktop video only: no audio, no webcam. Whole desktop or one monitor.

    python recorder.py                 # GUI (Start / Stop, Ctrl+Shift+F9 hotkey)
    python recorder.py --cli           # record until Enter / Ctrl+C
    python recorder.py --cli --fps 60 --monitor 1 --duration 30 --out D:\\clips

Engine: ffmpeg (gdigrab -> libx264 MP4) when it is on PATH, which draws the
mouse cursor and encodes in real time.  Without ffmpeg it falls back to
Pillow ImageGrab + OpenCV VideoWriter (no cursor, larger files).
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

APP_NAME = "SBM Labs Screen Recorder"
DEFAULT_OUT_DIR = Path.home() / "Videos" / "sbmlabs_screen_recorder"
SETTINGS_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "sbmlabs_screen_recorder" / "settings.json"

# name -> (x264 crf, x264 preset)   lower crf = better picture, bigger file
QUALITY_PRESETS = {
    "Balanced": (23, "veryfast"),
    "High": (18, "faster"),
    "Small file": (28, "veryfast"),
}
FPS_CHOICES = (15, 24, 30, 60)

HOTKEY_ID = 1
HOTKEY_LABEL = "Ctrl+Shift+F9"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_F9 = 0x78
WM_HOTKEY = 0x0312


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


def list_monitors() -> list[tuple[int, int, int, int]]:
    """[(left, top, width, height), ...] for every attached monitor."""
    rects: list[tuple[int, int, int, int]] = []
    proc_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def _cb(_hmon, _hdc, prect, _lparam):
        r = prect.contents
        rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_t(_cb), 0)
    rects.sort(key=lambda r: (r[0], r[1]))
    return rects


def virtual_desktop() -> tuple[int, int, int, int]:
    u = ctypes.windll.user32
    return u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)


def capture_targets() -> list[tuple[str, tuple[int, int, int, int] | None]]:
    """Dropdown entries: 'Full desktop' (None = whole virtual screen) then each monitor."""
    mons = list_monitors()
    targets: list[tuple[str, tuple[int, int, int, int] | None]] = [("Full desktop", None)]
    if len(mons) > 1:
        for i, (x, y, w, h) in enumerate(mons, 1):
            targets.append((f"Monitor {i}  ({w}x{h} @ {x},{y})", (x, y, w, h)))
    return targets


def new_output_path(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"sbmlabs_rec_{datetime.now():%Y%m%d_%H%M%S}.mp4"


# ---------------------------------------------------------------- engines ---
class FfmpegRecorder:
    """gdigrab -> libx264.  Stopped by sending 'q' so the MP4 is finalised."""

    name = "ffmpeg (gdigrab + libx264)"

    def __init__(self, ffmpeg: str) -> None:
        self.ffmpeg = ffmpeg
        self.proc: subprocess.Popen | None = None
        self.path: Path | None = None

    def start(self, path: Path, fps: int, quality: str, region: tuple[int, int, int, int] | None) -> None:
        crf, preset = QUALITY_PRESETS[quality]
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1",
               "-rtbufsize", "256M", "-thread_queue_size", "512"]
        if region is not None:
            x, y, w, h = region
            cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
        cmd += ["-i", "desktop",
                # yuv420p needs even dimensions; odd desktop sizes get cropped by 1px
                "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(path)]
        self.path = path
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # ffmpeg exits within a second if the args are wrong; catch that early
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
    """Fallback: ImageGrab frames written by OpenCV at a real-time paced fps."""

    name = "Pillow ImageGrab + OpenCV (no cursor)"

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: Exception | None = None
        self.path: Path | None = None

    def start(self, path: Path, fps: int, quality: str, region: tuple[int, int, int, int] | None) -> None:
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

    def _run(self, path: Path, fps: int, region: tuple[int, int, int, int] | None) -> None:
        import cv2
        import numpy as np
        from PIL import ImageGrab

        try:
            if region is None:
                x, y, w, h = virtual_desktop()
            else:
                x, y, w, h = region
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
class RecorderApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.engine = pick_engine()
        self.targets = capture_targets()
        self.events: queue.Queue[str] = queue.Queue()
        self.recording = False
        self.busy = False
        self.started_at = 0.0
        self.last_file: Path | None = None

        s = load_settings()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.v_target = tk.StringVar(value=self.targets[0][0])
        self.v_fps = tk.IntVar(value=int(s.get("fps", 30)))
        self.v_quality = tk.StringVar(value=s.get("quality", "Balanced"))
        self.v_out = tk.StringVar(value=s.get("out_dir", str(DEFAULT_OUT_DIR)))
        self.v_minimize = tk.BooleanVar(value=bool(s.get("minimize", True)))
        self.v_status = tk.StringVar(value="Ready")
        self.v_clock = tk.StringVar(value="00:00:00")
        self.v_last = tk.StringVar(value="")
        self.v_hotkey = tk.StringVar(value="")

        self._build()
        hot = start_hotkey_listener(lambda: self.events.put("toggle"))
        self.v_hotkey.set(f"{HOTKEY_LABEL} starts / stops" if hot else f"{HOTKEY_LABEL} is taken by another app")
        self.root.after(100, self._poll)

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        f = ttk.Frame(self.root, padding=12)
        f.grid(sticky="nsew")

        ttk.Label(f, text=APP_NAME, font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.l_status = ttk.Label(f, textvariable=self.v_status, font=("Segoe UI", 10, "bold"))
        self.l_status.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(f, textvariable=self.v_clock, font=("Consolas", 12)).grid(row=1, column=2, sticky="e")

        ttk.Label(f, text="Capture").grid(row=2, column=0, sticky="w", pady=(8, 2))
        self.c_target = ttk.Combobox(f, textvariable=self.v_target, state="readonly", width=30,
                                     values=[t[0] for t in self.targets])
        self.c_target.grid(row=2, column=1, columnspan=2, sticky="we", pady=(8, 2))

        ttk.Label(f, text="FPS").grid(row=3, column=0, sticky="w", pady=2)
        row = ttk.Frame(f)
        row.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        self.c_fps = ttk.Combobox(row, textvariable=self.v_fps, state="readonly", width=5, values=FPS_CHOICES)
        self.c_fps.pack(side="left")
        ttk.Label(row, text="   Quality").pack(side="left")
        self.c_quality = ttk.Combobox(row, textvariable=self.v_quality, state="readonly", width=11,
                                      values=list(QUALITY_PRESETS))
        self.c_quality.pack(side="left", padx=(6, 0))

        ttk.Label(f, text="Save to").grid(row=4, column=0, sticky="w", pady=2)
        self.e_out = ttk.Entry(f, textvariable=self.v_out, width=34)
        self.e_out.grid(row=4, column=1, sticky="we", pady=2)
        self.b_browse = ttk.Button(f, text="Browse", width=8, command=self.browse)
        self.b_browse.grid(row=4, column=2, sticky="e", padx=(6, 0), pady=2)

        ttk.Checkbutton(f, text="Minimise this window while recording", variable=self.v_minimize).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(f, textvariable=self.v_hotkey, foreground="#555").grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 8))

        self.b_toggle = tk.Button(f, text="\u25cf  Start recording", command=self.toggle, height=2,
                                  bg="#c62828", fg="white", activebackground="#b71c1c", activeforeground="white",
                                  font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2")
        self.b_toggle.grid(row=7, column=0, columnspan=2, sticky="we")
        ttk.Button(f, text="Open folder", command=self.open_folder).grid(row=7, column=2, sticky="we", padx=(6, 0))

        ttk.Label(f, textvariable=self.v_last, foreground="#555", wraplength=330, justify="left").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(f, text=f"Engine: {self.engine.name}", foreground="#888", font=("Segoe UI", 8)).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))
        f.columnconfigure(1, weight=1)

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

    def toggle(self) -> None:
        if self.busy:
            return
        self.stop() if self.recording else self.start()

    def _set_inputs(self, enabled: bool) -> None:
        for w in (self.c_target, self.c_fps, self.c_quality):
            w.configure(state="readonly" if enabled else "disabled")
        self.e_out.configure(state="normal" if enabled else "disabled")
        self.b_browse.configure(state="normal" if enabled else "disabled")

    def start(self) -> None:
        from tkinter import messagebox
        out_dir = Path(self.v_out.get().strip() or DEFAULT_OUT_DIR)
        region = dict(self.targets)[self.v_target.get()]
        try:
            path = new_output_path(out_dir)
            self.engine.start(path, int(self.v_fps.get()), self.v_quality.get(), region)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=self.root)
            return
        save_settings({"fps": int(self.v_fps.get()), "quality": self.v_quality.get(),
                       "out_dir": str(out_dir), "minimize": bool(self.v_minimize.get())})
        self.recording = True
        self.started_at = time.monotonic()
        self.v_status.set("\u25cf  RECORDING")
        self.l_status.configure(foreground="#c62828")
        self.v_last.set(f"Writing {path.name}")
        self.b_toggle.configure(text="\u25a0  Stop recording", bg="#37474f", activebackground="#263238")
        self._set_inputs(False)
        if self.v_minimize.get():
            self.root.iconify()

    def stop(self) -> None:
        self.busy = True
        self.v_status.set("Finalising\u2026")
        self.b_toggle.configure(state="disabled")
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self) -> None:
        try:
            path = self.engine.stop()
            self.events.put(f"done:{path}")
        except Exception as e:
            self.events.put(f"error:{e}")

    def _on_stopped(self, msg: str) -> None:
        from tkinter import messagebox
        self.recording = False
        self.busy = False
        self.b_toggle.configure(state="normal", text="\u25cf  Start recording", bg="#c62828", activebackground="#b71c1c")
        self._set_inputs(True)
        self.l_status.configure(foreground="")
        if self.root.state() == "iconic":
            self.root.deiconify()
        if msg.startswith("done:"):
            self.last_file = Path(msg[5:])
            size = self.last_file.stat().st_size / 1e6 if self.last_file.exists() else 0
            self.v_status.set("Saved")
            self.v_last.set(f"Saved {self.last_file}  ({size:.1f} MB)")
        else:
            self.v_status.set("Failed")
            self.v_last.set(msg[6:])
            messagebox.showerror(APP_NAME, msg[6:], parent=self.root)

    def _poll(self) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev == "toggle":
                self.toggle()
            else:
                self._on_stopped(ev)
        if self.recording:
            secs = int(time.monotonic() - self.started_at)
            self.v_clock.set(f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}")
            if not self.busy and not self.engine.alive():
                self.stop()  # encoder died -> surface its error
        self.root.after(200, self._poll)

    def on_close(self) -> None:
        if self.recording:
            try:
                self.engine.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


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
    path = new_output_path(Path(args.out))
    print(f"{APP_NAME}  --  {engine.name}")
    print(f"Capturing {label} at {args.fps} fps, quality {args.quality}")
    print(f"Writing   {path}")
    try:
        engine.start(path, args.fps, args.quality, region)
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
    p.add_argument("--fps", type=int, default=30, choices=FPS_CHOICES)
    p.add_argument("--quality", default="Balanced", choices=list(QUALITY_PRESETS))
    p.add_argument("--monitor", type=int, default=0, help="0 = full desktop, 1.. = that monitor")
    p.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="folder for the MP4 files")
    p.add_argument("--duration", type=float, default=0, help="seconds to record (CLI only; 0 = until stopped)")
    args = p.parse_args()

    _make_dpi_aware()
    if args.cli:
        return run_cli(args)
    RecorderApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
