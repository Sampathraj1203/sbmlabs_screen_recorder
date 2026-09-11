# SBM Labs Screen Recorder

Records to **MP4 (H.264 / AAC)** from any combination of three sources, each an
optional tick:

* **Desktop** -- whole screen or one monitor, with the mouse cursor
* **Camera** -- any webcam, drawn **top-right as a small picture-in-picture** over
  the desktop (or full-frame when the desktop is off)
* **Audio** -- any input device (microphone, USB audio, Stereo Mix) with mic boost

Modern dark UI, global hotkey, CLI mode. Audio-only recordings are saved as `.m4a`.

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
| **Record desktop** tick | Off = no screen capture (camera and/or audio only). |
| Capture | `Full desktop`, or one monitor when more than one is attached. |
| **Record camera** tick | On = the webcam is recorded. With the desktop on it sits top-right as a picture-in-picture with a white frame; with the desktop off it fills the frame. |
| Small / Medium / Large | Width of the picture-in-picture: 16 / 22 / 30 % of the frame. |
| Preview | Opens a small always-on-top window showing the camera so you can frame yourself. Closes by itself when a recording starts (a camera cannot be opened twice). |
| Camera | Any DirectShow webcam. |
| **Record audio** tick | Off = video only. On = the audio controls light up. |
| Audio input | Any DirectShow input device (mic, USB audio, Stereo Mix). `↻` re-scans cameras and mics after plugging something in. |
| **Test mic** | Live level meter for the chosen device -- speak and watch the bar (green / amber / red near clipping). Click again to stop; it stops by itself when a recording starts. |
| Audio quality | `Standard` = AAC 160 kb/s, 44.1 kHz. `High` = AAC 320 kb/s, 48 kHz. |
| **Mic boost** | 0 to +30 dB gain on the mic (default +20 dB -- USB mics on Windows usually come in far too quiet). The Test-mic bar shows the boosted level, so speak and drag until normal speech sits in the green/amber. |
| Auto level | On (default): the boost is the *maximum* -- quiet speech is lifted by up to that much and loud parts are pulled down, so the level stays even. Off: a plain fixed gain. A limiter stops clipping either way. |
| Frame rate | 15 / 24 / 30 / 60. 30 is the sensible default. |
| Quality | `Small file` (crf 28), `Balanced` (crf 23), `High` (crf 18, bigger file). |
| Save to | Folder for the clips. Default `%USERPROFILE%\Videos\sbmlabs_screen_recorder`. |
| Open folder / Play last | Opens the folder in Explorer / plays the clip you just made. |

Files are named `sbmlabs_rec_YYYYMMDD_HHMMSS.mp4` (`.m4a` for audio only). Every
choice -- source ticks, devices, PiP size, audio quality, mic boost, auto level,
FPS, quality, folder, minimise -- is remembered in
`%LOCALAPPDATA%\sbmlabs_screen_recorder\settings.json`.

## CLI

```powershell
python recorder.py --list-devices                           # cameras and audio inputs
python recorder.py --cli --camera default                   # desktop + webcam PiP top-right
python recorder.py --cli --camera default --camera-size Large --audio default
python recorder.py --cli --no-desktop --camera default --audio default   # webcam only
python recorder.py --cli --no-desktop --audio default       # audio only -> .m4a
python recorder.py --cli                                    # video only, until Enter / Ctrl+C
python recorder.py --cli --audio default                    # + first microphone found
python recorder.py --cli --audio "USB PnP" --duration 60    # device by (part of) its name, 60 s
python recorder.py --cli --audio default --audio-quality High   # AAC 320k / 48 kHz
python recorder.py --cli --audio default --mic-gain 25           # boost (default 20 dB)
python recorder.py --cli --audio default --no-auto-level         # fixed gain instead of adaptive
python recorder.py --cli --fps 60 --quality High --out D:\clips
python recorder.py --cli --monitor 1                        # 0 = full desktop, 1.. = that monitor
```

## How it records

* **ffmpeg** (on PATH -- the winget `Gyan.FFmpeg` build is): `gdigrab` grabs the
  desktop with the mouse cursor, `dshow` grabs the webcam and the audio device
  (50 ms buffer to keep it tight against the video), an `overlay` filter draws the
  camera top-right, `libx264` + `aac` encode live into one MP4. Stopping sends `q` to ffmpeg so the
  file is finalised properly (`+faststart`, plays anywhere). Roughly 10-15 MB per
  minute at 1080p / 30 fps / Balanced, mostly depending on how much moves on
  screen.
* **Fallback** when ffmpeg is missing: Pillow `ImageGrab` + OpenCV `VideoWriter`
  (mpeg4). Desktop only -- no cursor, no audio, no camera, larger files. Installing ffmpeg is
  the fix: `winget install Gyan.FFmpeg`.

Only `customtkinter` is needed for the GUI; the CLI is pure standard library
(plus ffmpeg). The fallback engine needs `pillow`, `opencv-python`, `numpy`.

## Notes

* **Still too quiet?** Raise the mic in Windows too: Settings -> System -> Sound ->
  Input -> your microphone -> *Input volume* to 100, and in the device's
  *Additional device properties* enable *Microphone Boost* if it has one. The
  app's boost stacks on top of that.
* To record **what you hear** (system audio), enable *Stereo Mix* in Windows
  Sound settings -> Recording; it then shows up as an audio input here.
* If the status line says the hotkey *is taken by another app*, some other
  program owns Ctrl+Shift+F9; the Start/Stop button still works.
* Odd-sized capture areas are cropped by one pixel (H.264 needs even
  dimensions).
* The camera dropdown lists only cameras ffmpeg can open. *OBS Virtual Camera*
  appears only while OBS is running.
* Closing the window while recording stops and finalises the clip first.
