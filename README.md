# SBM Labs Screen Recorder

Records the Windows desktop to an **MP4 (H.264)** file. Desktop video only:
no audio, no webcam. Whole desktop or a single monitor.

## Run it

Double-click **`sbmlabs_screen_recorder.bat`** (opens the window with no
console), or:

```powershell
python recorder.py            # GUI
python recorder.py --cli      # terminal mode, Enter / Ctrl+C stops
```

Make a desktop shortcut to the `.bat` if you want it one click away.

## GUI

| Control | What it does |
|---|---|
| **Start / Stop recording** | Toggles. The window minimises itself while recording (untick the box to keep it on top instead). |
| **Ctrl+Shift+F9** | Global hotkey -- starts / stops from anywhere, no need to find the window. |
| Capture | `Full desktop`, or one monitor when more than one is attached. |
| FPS | 15 / 24 / 30 / 60. 30 is the sensible default. |
| Quality | `Balanced` (crf 23), `High` (crf 18, bigger file), `Small file` (crf 28). |
| Save to | Folder for the clips. Default `%USERPROFILE%\Videos\sbmlabs_screen_recorder`. |
| Open folder | Opens that folder in Explorer. |

Files are named `sbmlabs_rec_YYYYMMDD_HHMMSS.mp4`. FPS, quality, folder and
the minimise choice are remembered in
`%LOCALAPPDATA%\sbmlabs_screen_recorder\settings.json`.

## CLI

```powershell
python recorder.py --cli                                  # until Enter / Ctrl+C
python recorder.py --cli --duration 60                    # fixed 60 s
python recorder.py --cli --fps 60 --quality High --out D:\clips
python recorder.py --cli --monitor 1                      # 0 = full desktop, 1.. = that monitor
```

## How it records

* **ffmpeg** (on PATH -- the winget `Gyan.FFmpeg` build is): `gdigrab` grabs the
  desktop with the mouse cursor and `libx264` encodes it live. Stopping sends
  `q` to ffmpeg so the MP4 is finalised properly (`+faststart`, plays anywhere).
  Roughly 10-15 MB per minute at 1080p / 30 fps / Balanced, mostly depending on
  how much moves on screen.
* **Fallback** when ffmpeg is missing: Pillow `ImageGrab` + OpenCV `VideoWriter`
  (mpeg4). Works, but no cursor, larger files and it is CPU-heavier at high fps.
  Installing ffmpeg is the fix: `winget install Gyan.FFmpeg`.

Only the standard library is needed for the ffmpeg path; the fallback needs
`pillow`, `opencv-python`, `numpy`.

## Notes

* If the status line says the hotkey *is taken by another app*, some other
  program owns Ctrl+Shift+F9; the Start/Stop button still works.
* Odd-sized capture areas are cropped by one pixel (H.264 needs even
  dimensions).
* Closing the window while recording stops and finalises the clip first.
