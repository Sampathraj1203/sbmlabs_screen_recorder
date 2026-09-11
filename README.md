# SBM Labs Screen Recorder

Records the Windows desktop to an **MP4 (H.264)** file, with optional **audio
from any input device** (microphone, USB audio, Stereo Mix). Whole desktop or a
single monitor. Modern dark UI, global hotkey, CLI mode.

## Run it

Double-click **`sbmlabs_screen_recorder.bat`** (opens the window with no
console; installs the UI toolkit on the first run), or:

```powershell
python -m pip install -r requirements.txt   # once: customtkinter
python recorder.py                          # GUI
python recorder.py --cli                    # terminal mode, Enter / Ctrl+C stops
```

Make a desktop shortcut to the `.bat` if you want it one click away.

## GUI

| Control | What it does |
|---|---|
| **Start / Stop recording** | Toggles. The window minimises itself while recording (switch it off to keep the window on top instead). |
| **Ctrl+Shift+F9** | Global hotkey -- starts / stops from anywhere, no need to find the window. |
| Capture | `Full desktop`, or one monitor when more than one is attached. |
| Audio input | `No audio` or any DirectShow input device. `↻` re-scans after plugging in a mic. |
| Frame rate | 15 / 24 / 30 / 60. 30 is the sensible default. |
| Quality | `Small file` (crf 28), `Balanced` (crf 23), `High` (crf 18, bigger file). |
| Save to | Folder for the clips. Default `%USERPROFILE%\Videos\sbmlabs_screen_recorder`. |
| Open folder / Play last | Opens the folder in Explorer / plays the clip you just made. |

Files are named `sbmlabs_rec_YYYYMMDD_HHMMSS.mp4`. FPS, quality, folder, audio
device and the minimise choice are remembered in
`%LOCALAPPDATA%\sbmlabs_screen_recorder\settings.json`.

## CLI

```powershell
python recorder.py --list-audio                             # show input devices
python recorder.py --cli                                    # video only, until Enter / Ctrl+C
python recorder.py --cli --audio default                    # + first microphone found
python recorder.py --cli --audio "USB PnP" --duration 60    # device by (part of) its name, 60 s
python recorder.py --cli --fps 60 --quality High --out D:\clips
python recorder.py --cli --monitor 1                        # 0 = full desktop, 1.. = that monitor
```

## How it records

* **ffmpeg** (on PATH -- the winget `Gyan.FFmpeg` build is): `gdigrab` grabs the
  desktop with the mouse cursor, `dshow` grabs the audio device, `libx264` +
  `aac` (160 kb/s) encode live into one MP4. Stopping sends `q` to ffmpeg so the
  file is finalised properly (`+faststart`, plays anywhere). Roughly 10-15 MB per
  minute at 1080p / 30 fps / Balanced, mostly depending on how much moves on
  screen.
* **Fallback** when ffmpeg is missing: Pillow `ImageGrab` + OpenCV `VideoWriter`
  (mpeg4). Video only -- no cursor, no audio, larger files. Installing ffmpeg is
  the fix: `winget install Gyan.FFmpeg`.

Only `customtkinter` is needed for the GUI; the CLI is pure standard library
(plus ffmpeg). The fallback engine needs `pillow`, `opencv-python`, `numpy`.

## Notes

* To record **what you hear** (system audio), enable *Stereo Mix* in Windows
  Sound settings -> Recording; it then shows up as an audio input here.
* If the status line says the hotkey *is taken by another app*, some other
  program owns Ctrl+Shift+F9; the Start/Stop button still works.
* Odd-sized capture areas are cropped by one pixel (H.264 needs even
  dimensions).
* Closing the window while recording stops and finalises the clip first.
