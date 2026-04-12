# Sort Media Pool — by DrRave
Automatically organises your DaVinci Resolve Media Pool by camera model and media type.

## What it does
- Detects camera make and model from file metadata
- Creates bins automatically (Canon EOS R5, Sony FX3 etc)
- Separates audio, images, RAW stills, timelines
- Supports multi-camera shoots (A Cam / B Cam)
- Works with BRAW, MP4, MOV, MXF, R3D and more

## Cameras supported
Blackmagic (all models), Canon EOS, Sony FX/Alpha,
Nikon Z series, Leica SL/Q, GoPro, DJI, ARRI ALEXA,
RED, Nikon ZR, Panasonic Lumix, Apple iPhone,
Android phones (OnePlus, Samsung, Google Pixel etc)

## Install — 2 steps

### Mac
1. Copy the DrRave folder to:
   `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`

2. Restart DaVinci Resolve

### Windows
1. Copy the DrRave folder to:
   `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`

2. Restart DaVinci Resolve

## Use
1. Open DaVinci Resolve Studio
2. Import your footage into the Media Pool
3. Workspace → Scripts → DrRave → Sort Media Pool
4. Done

## Dry run (preview without changes)
The script runs normally by default.
To preview without moving anything, open the script
and change `DRY_RUN = False` to `DRY_RUN = True` at the top.

## Customise camera detection
Edit `camera_patterns.json` to add your own filename patterns.
See the `_readme` key inside the file for instructions.

## Updates
Check drrave.com/sort-media-pool for the latest version.
The script checks for updates automatically on each run.

## Support
drrave.com | support@drrave.com

## Version history
v1.0.0 — Initial release

---
© 2026 Dr. Ravi Tahilramani — drrave.com
