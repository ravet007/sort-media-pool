# Sort Media Pool — by DrRave
Automatically organises your DaVinci Resolve Media Pool by camera model and media type.

> **Requires DaVinci Resolve Studio (paid version).** The free version of DaVinci Resolve does not support external scripting.

---

## What it does

- Detects camera make and model directly from file metadata
- Creates bins automatically — Canon EOS R5, Sony FX3, Blackmagic PYXIS 6K etc
- Separates audio, images, RAW stills, timelines and compound clips
- Supports multi-camera shoots — A Cam / B Cam, Cam 1 / Cam 2
- Works with BRAW, MP4, MOV, MXF, R3D and more
- Checks for updates automatically on every run

## Cameras supported

Blackmagic (all models), Canon EOS, Sony FX/Alpha, Nikon Z series, Leica SL/Q, GoPro HERO, DJI Drone, ARRI ALEXA, RED, Nikon ZR, Panasonic Lumix, Apple iPhone, Android phones (OnePlus, Samsung, Google Pixel, Xiaomi, Vivo, Oppo and more)

## Requirements

- DaVinci Resolve Studio (not the free version)
- macOS or Windows
- Internet connection for update checks (optional)

---

## Before you install — Enable External Scripting

Sort Media Pool requires external scripting to be enabled in Resolve. This is a one-time setup.

1. Open DaVinci Resolve Studio
2. Go to **DaVinci Resolve → Preferences → System → General**
3. Set **External scripting using** to **Local**
4. Click **Save**
5. Restart DaVinci Resolve

---

## Install — Mac

1. Download `DrRave_Sort_Media_Pool.zip` from drrave.com/tools/sort-media-pool
2. Unzip the file
3. Copy the `DrRave` folder to:
   ```
   ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
   ```
4. Restart DaVinci Resolve

## Install — Windows

1. Download `DrRave_Sort_Media_Pool.zip` from drrave.com/tools/sort-media-pool
2. Unzip the file
3. Copy the `DrRave` folder to:
   ```
   %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\
   ```
4. Restart DaVinci Resolve

---

## How to use

1. Open DaVinci Resolve Studio
2. Open a project and import your footage into the Media Pool
3. Go to **Workspace → Scripts → DrRave → Sort_Media_Pool**
4. The script runs automatically and organises your Media Pool

---

## Updating to a new version

When a new version is available, a popup will appear inside Resolve after running the script. To update:

1. Visit drrave.com/tools/sort-media-pool
2. Enter your email and download the new zip
3. Unzip — find `Sort_Media_Pool.py` inside the `DrRave` folder
4. Replace the old `Sort_Media_Pool.py` in your installed `DrRave` folder (same location as install above)
5. Restart DaVinci Resolve

> You do not need to replace the `ffmpeg` folder — only `Sort_Media_Pool.py` changes between updates.

---

## Customise camera detection

Edit `camera_patterns.json` to add your own filename patterns. Open the file in any text editor — the `_readme` key inside explains how.

---

## Support

drrave.com | support@drrave.com

---

## Version history

- v1.0.0 — Initial release

---

© 2026 Dr. Ravi Tahilramani — drrave.com
