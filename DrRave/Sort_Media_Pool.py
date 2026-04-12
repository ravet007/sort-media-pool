# -*- coding: utf-8 -*-
# Requires Python 3.6+
#!/usr/bin/env python3
# Sort Media Pool
# Automatically organises your Media Pool by camera and media type
# Version: 1.0.0
# Author: DrRave — drrave.com
"""
Sort_Media_Pool.py — Organise DaVinci Resolve Media Pool by camera make/model
and media type.

Standalone Resolve Script. Install in:
  Mac: ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/DrRave/
  Win: %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\DrRave\\

Usage via Resolve menu:
  Workspace → Scripts → DrRave → Sort Media Pool

Command line:
  python3 Sort_Media_Pool.py            # organise the Media Pool
  python3 Sort_Media_Pool.py --dry-run  # preview without making changes
"""

import sys
if sys.version_info[0] < 3:
    raise RuntimeError(
        "Sort Media Pool requires Python 3. "
        "Please ensure Python 3 is installed."
    )

import argparse
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

try:
    import exifread
    EXIF_AVAILABLE = True
except ImportError:
    EXIF_AVAILABLE = False


# ── Version ───────────────────────────────────────────────────────────────────

VERSION = "1.0.0"


# ── OS-aware path helpers ─────────────────────────────────────────────────────

def _get_fusionscript_path() -> str:
    system = platform.system()
    if system == "Darwin":
        return (
            "/Applications/DaVinci Resolve/DaVinci Resolve.app"
            "/Contents/Libraries/Fusion/fusionscript.so"
        )
    if system == "Windows":
        return (
            "C:\\Program Files\\Blackmagic Design\\"
            "DaVinci Resolve\\fusionscript.dll"
        )
    raise OSError(f"Unsupported OS: {system}")


def _get_script_dir() -> Path:
    """
    Return the DrRave script directory.

    Works in both execution contexts:
      - Terminal: __file__ is defined, use its parent directly.
      - Resolve menu: __file__ is not defined; search known install
        locations on Mac and Windows, falling back to cwd.
    """
    # Terminal execution
    try:
        return Path(__file__).parent
    except NameError:
        pass

    # Resolve menu execution — __file__ not available
    system = platform.system()
    if system == "Darwin":
        candidates = [
            # User install (most common)
            Path(os.path.expanduser("~")) / (
                "Library/Application Support/Blackmagic Design"
                "/DaVinci Resolve/Fusion/Scripts/Utility/DrRave"
            ),
            # System-wide install
            Path(
                "/Library/Application Support/Blackmagic Design"
                "/DaVinci Resolve/Fusion/Scripts/Utility/DrRave"
            ),
        ]
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        candidates = [
            Path(appdata) / (
                "Blackmagic Design/DaVinci Resolve/Support"
                "/Fusion/Scripts/Utility/DrRave"
            ),
        ]
    else:
        candidates = []

    for p in candidates:
        if p.exists():
            return p

    return Path.cwd()


def _get_ffprobe_path() -> str | None:
    """
    Resolve the ffprobe binary path.

    Priority:
      1. Bundled binary in ffmpeg/mac/ or ffmpeg/win/ relative to the
         script directory (works from terminal and Resolve menu).
         Auto-chmod'd on Mac; macOS quarantine attribute cleared so
         Gatekeeper does not block the first run.
      2. System ffprobe found via PATH (shutil.which).
      3. None — caller shows a warning and runs extension-only detection.
    """
    script_dir = _get_script_dir()
    system     = platform.system()

    if system == "Darwin":
        # Check both the resolved script dir and both known install locations
        mac_candidates = [
            script_dir / "ffmpeg" / "mac" / "ffprobe",
            Path(os.path.expanduser("~")) / (
                "Library/Application Support/Blackmagic Design"
                "/DaVinci Resolve/Fusion/Scripts/Utility/DrRave"
                "/ffmpeg/mac/ffprobe"
            ),
            Path(
                "/Library/Application Support/Blackmagic Design"
                "/DaVinci Resolve/Fusion/Scripts/Utility/DrRave"
                "/ffmpeg/mac/ffprobe"
            ),
        ]
        for bundled in mac_candidates:
            if bundled.exists():
                # Ensure executable bit is set
                bundled.chmod(bundled.stat().st_mode | stat.S_IEXEC)
                # Remove macOS quarantine flag so Gatekeeper won't block it
                try:
                    subprocess.run(
                        ["xattr", "-d", "com.apple.quarantine", str(bundled)],
                        capture_output=True,
                    )
                except Exception:
                    pass
                return str(bundled)

    elif system == "Windows":
        win_candidates = [
            script_dir / "ffmpeg" / "win" / "ffprobe.exe",
            Path(os.environ.get("APPDATA", "")) / (
                "Blackmagic Design/DaVinci Resolve/Support"
                "/Fusion/Scripts/Utility/DrRave/ffmpeg/win/ffprobe.exe"
            ),
        ]
        for bundled in win_candidates:
            if bundled.exists():
                return str(bundled)

    # Fallback: system ffprobe on PATH
    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        return system_ffprobe

    return None


# ── Constants ─────────────────────────────────────────────────────────────────

FUSIONSCRIPT_PATH = _get_fusionscript_path()
FFPROBE_PATH      = _get_ffprobe_path()

BRAW_EXTS       = {".braw"}
RED_EXTS        = {".r3d"}
CANON_EXTS      = {".crm"}
MXF_EXTS        = {".mxf"}
AUDIO_EXTS      = {".wav", ".aif", ".aiff", ".mp3", ".aac", ".flac"}
GRAPHIC_EXTS    = {".psd", ".ai", ".svg"}
IMAGE_EXTS      = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dpx", ".exr"}
RAW_STILL_EXTS  = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf",
                   ".rw2", ".orf", ".pef", ".srw"}

# RED sidecar extensions — binary thumbnail/index files with no camera metadata.
# Excluded from Media Pool scanning so they never appear as Unknown clips.
RED_SIDECAR_EXTS = {".rtn", ".rmd", ".rim", ".rpx"}


# ── Resolve connection ────────────────────────────────────────────────────────

def _load_fusionscript():
    loader = importlib.machinery.ExtensionFileLoader("fusionscript", FUSIONSCRIPT_PATH)
    spec   = importlib.util.spec_from_loader("fusionscript", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def get_resolve():
    if not os.path.isfile(FUSIONSCRIPT_PATH):
        raise FileNotFoundError(f"fusionscript not found at: {FUSIONSCRIPT_PATH}")
    fusionscript = _load_fusionscript()
    if hasattr(fusionscript, "scriptapp"):
        return fusionscript.scriptapp("Resolve")
    if hasattr(fusionscript, "bmd"):
        return fusionscript.bmd.scriptapp("Resolve")
    raise RuntimeError("fusionscript has neither 'scriptapp' nor 'bmd'")


# ── Update check ──────────────────────────────────────────────────────────────

def check_for_updates(current_version: str, resolve=None, fusion=None) -> None:
    """
    Silently check GitHub for a newer version.

    When an update is found, tries four approaches in order:
      1. resolve.ShowMessage()        — Resolve native popup
      2. fusion.ShowMessage()         — Fusion native popup
      3. Fusion UI Manager dialog     — custom Fusion dialog window
      4. osascript display dialog     — macOS native dialog (most reliable on Mac)
      5. Console print                — terminal fallback
    Any network or parse failure is swallowed so an offline machine is unaffected.
    """
    try:
        url = (
            "https://raw.githubusercontent.com/ravet007/"
            "sort-media-pool/main/version.json"
        )
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
        latest = data.get("version", "")
        notes  = data.get("release_notes", "")

        if not latest or latest == current_version:
            return

        message = (
            f"Sort Media Pool update available!\n\n"
            f"New version: {latest}\n"
            f"{notes}\n\n"
            f"Visit drrave.com/sort-media-pool to download."
        )

        shown = False

        # Approach 1 — Resolve native popup
        if not shown and resolve:
            try:
                resolve.ShowMessage(message)
                shown = True
            except Exception:
                pass

        # Approach 2 — Fusion ShowMessage
        if not shown and fusion:
            try:
                fusion.ShowMessage(message)
                shown = True
            except Exception:
                pass

        # Approach 3 — Fusion UI Manager dialog
        if not shown and fusion:
            try:
                ui   = fusion.UIManager
                disp = bmd.UIDispatcher(ui)
                win  = ui.Window(
                    {
                        "ID": "UpdateDialog",
                        "WindowTitle": "Sort Media Pool — Update Available",
                        "Geometry": [400, 300, 400, 200],
                        "Events": {"Close": True},
                    },
                    [
                        ui.VGroup([
                            ui.Label({"ID": "Msg", "Text": message, "WordWrap": True}),
                            ui.Button({"ID": "OK", "Text": "OK"}),
                        ])
                    ],
                )

                def on_close(ev):
                    disp.ExitLoop()

                def on_ok(ev):
                    disp.ExitLoop()

                win.On.UpdateDialog.Close = on_close
                win.On.OK.Click           = on_ok
                win.Show()
                disp.RunLoop()
                win.Hide()
                shown = True
            except Exception:
                pass

        # Approach 4 — macOS native dialog via osascript
        if not shown and platform.system() == "Darwin":
            try:
                safe = message.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.run(
                    [
                        "osascript", "-e",
                        f'display dialog "{safe}" '
                        f'buttons {{"OK"}} '
                        f'default button "OK" '
                        f'with title "Sort Media Pool Update"',
                    ],
                    timeout=30,
                )
                shown = True
            except Exception:
                pass

        # Approach 5 — terminal fallback
        if not shown:
            print(f"  ⚡ Update available: v{latest}")
            print(f"     {notes}")
            print(f"     drrave.com/sort-media-pool")

    except Exception:
        pass


# ── ffprobe helpers ───────────────────────────────────────────────────────────

def check_ffprobe() -> bool:
    """Return True if a usable ffprobe binary was found."""
    if not FFPROBE_PATH:
        return False
    try:
        r = subprocess.run(
            [FFPROBE_PATH, "-version"],
            capture_output=True, timeout=3
        )
        return r.returncode == 0
    except Exception:
        return False


def run_ffprobe(file_path: str) -> dict | None:
    """
    Run ffprobe on file_path and return parsed JSON, or None on any failure.

    Uses a two-pass strategy:

    Pass 1: -show_format -show_streams
        Needed for cameras that embed make/model in stream-level tags
        (e.g. some Sony and Panasonic MOV/MXF files).

    Pass 2: -show_format only (fallback)
        # BRAW DETECTION NOTE:
        # ffprobe with -show_streams fails silently on BRAW files because
        # the BRAW codec cannot be decoded by ffprobe's stream header parser.
        # Fix: always retry with -show_format only if the first probe
        # returns no useful metadata. This catches BRAW and other
        # proprietary containers (RED, CRM etc) that embed metadata
        # at the format/container level rather than stream level.
        #
        # Detection order for BRAW:
        # 1. Full probe (-show_format -show_streams) → returns nothing
        # 2. Format-only retry (-show_format only) → returns camera_type,
        #    manufacturer, lens_type etc
        # 3. camera_type tag → exact model name
    """
    base_args = [FFPROBE_PATH, "-v", "quiet", "-print_format", "json"]

    for extra in (["-show_format", "-show_streams"], ["-show_format"]):
        try:
            r = subprocess.run(
                base_args + extra + [file_path],
                capture_output=True, timeout=3, text=True
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
            pass

    return None


def _is_encoder_string(name: str) -> bool:
    """Return True if the string is an FFmpeg encoder tag, not a camera name."""
    return bool(re.match(r'^Lavf|^LAV|^\d+\.\d+', name))


def _combine(tags: dict, make_key: str, model_key: str) -> str | None:
    make  = tags.get(make_key,  "").strip()
    model = tags.get(model_key, "").strip()
    result = f"{make} {model}".strip()
    return result if result else None


def load_config() -> dict:
    """Load camera_patterns.json from the script's directory, or return defaults.

    Keys starting with '_' are documentation notes for the user and are stripped
    before returning so they never accidentally match a config lookup.
    """
    config_path = _get_script_dir() / "camera_patterns.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Warning: Could not read camera_patterns.json — {e}")
    return {
        "filename_patterns": {},
        "minor_version_signatures": {},
        "firmware_signatures": {},
        "brand_codes": {},
    }


def extract_camera_from_tags(data: dict) -> str | None:
    """
    Extract camera name from ffprobe make/model tags only.  Priority order:
      1. format.tags.make + model
      2. format.tags.com.apple.quicktime.make + model
      3. format.tags.artist  (some Canon files)
      4. streams[0].tags.make + model
      5. streams[0].tags.com.apple.quicktime.make + model
    Encoder strings (Lavf*, LAV*, bare version numbers) are rejected.
    """
    fmt_tags    = data.get("format", {}).get("tags", {})
    streams     = data.get("streams", [])
    stream_tags = streams[0].get("tags", {}) if streams else {}

    candidates = [
        _combine(fmt_tags,    "make",                     "model"),
        _combine(fmt_tags,    "com.apple.quicktime.make", "com.apple.quicktime.model"),
        fmt_tags.get("artist", "").strip() or None,
        _combine(stream_tags, "make",                     "model"),
        _combine(stream_tags, "com.apple.quicktime.make", "com.apple.quicktime.model"),
    ]

    for name in candidates:
        if name and not _is_encoder_string(name):
            return name

    return None


def extract_company_product(data: dict) -> tuple[str, str] | None:
    """
    Extract camera name from MXF standard company_name / product_name tags.
    These are embedded by ARRI, Sony Venice, Canon Cinema and other
    professional cameras that shoot MXF.

    Returns (camera_name, method_string) or None if company_name is absent.

    Examples:
      company_name="ARRI"  product_name="ALEXA Mini"  → "ARRI ALEXA Mini"
      company_name="ARRI"  product_name=""             → "ARRI"
      product_version present                          → appended as "SUP X.X"
    """
    fmt_tags = data.get("format", {}).get("tags", {})
    company  = fmt_tags.get("company_name",   "").strip()
    product  = fmt_tags.get("product_name",   "").strip()
    version  = fmt_tags.get("product_version","").strip()

    if not company:
        return None

    camera = f"{company} {product}".strip() if product else company

    method_parts = ["via company_name + product_name" if product else "via company_name"]
    if version:
        # product_version may already include "SUP" — avoid "SUP SUP x.x"
        version_label = version if version.upper().startswith("SUP") else f"SUP {version}"
        method_parts.append(version_label)

    return camera, " | ".join(method_parts)


def extract_android_model(data: dict) -> tuple[str, str] | None:
    """
    Detect Android phone camera from vendor-specific model tags.

    Each Android manufacturer embeds proprietary namespace tags rather than
    the standard make/model fields.  Tags where fallback_name is None contain
    the exact model string directly; tags where fallback_name is set confirm
    the manufacturer but carry no model — the fallback_name is used instead.

    ANDROID_TAGS is checked in order: model tags first (most specific),
    manufacturer-confirm tags next, generic Android version last.
    """
    fmt_tags = data.get("format", {}).get("tags", {})

    # (tag_key, fallback_name)
    # fallback_name=None  → tag value IS the model name
    # fallback_name=str   → tag confirms manufacturer; use fallback_name as bin
    ANDROID_TAGS = [
        ("com.oplus.product.model",              None),            # OnePlus exact model
        ("com.samsung.model",                    None),            # Samsung exact model
        ("com.google.product.model",             None),            # Pixel exact model
        ("com.xiaomi.product.model",             None),            # Xiaomi exact model
        ("com.vivo.product.model",               None),            # Vivo exact model
        ("com.oppo.product.model",               None),            # Oppo exact model
        ("com.realme.product.model",             None),            # Realme exact model
        ("com.huawei.product.model",             None),            # Huawei exact model
        ("com.motorola.product.model",           None),            # Motorola exact model
        ("com.samsung.android.version.release",  "Samsung"),       # Samsung fallback
        ("com.miui.version",                     "Xiaomi"),        # Xiaomi fallback
        ("com.vivo.os.version",                  "Vivo"),          # Vivo fallback
        ("com.oppo.version",                     "Oppo"),          # Oppo fallback
        ("com.huawei.version",                   "Huawei"),        # Huawei fallback
        ("com.android.version",                  "Android Phone"), # generic fallback
    ]

    for tag_key, fallback_name in ANDROID_TAGS:
        value = fmt_tags.get(tag_key, "").strip()
        if value:
            camera = value if fallback_name is None else fallback_name
            return camera, "via Android model tag"

    return None


def detect_leica(data: dict) -> str | None:
    """
    Detect Leica cameras from MOV/MP4 container tags.

    Leica embeds 'leic' in compatible_brands and may also write
    L-Log metadata into com.apple.proapps.customcolor.
    No model info is available in video files — bin name = "Leica".
    """
    fmt      = data.get("format", {})
    fmt_tags = fmt.get("tags", {})

    brands = fmt_tags.get("compatible_brands", "") or fmt.get("compatible_brands", "")
    if "leic" in brands.lower():
        custom_color = fmt_tags.get("com.apple.proapps.customcolor", "").lower()
        method = "via brand code + L-Log tag" if "leica" in custom_color else "via brand code"
        return "Leica", method

    # Fallback: customcolor tag alone confirms Leica even without brand code
    if "leica" in fmt_tags.get("com.apple.proapps.customcolor", "").lower():
        return "Leica", "via L-Log tag"

    return None


_MAKE_NORMALISE = {
    "LEICA CAMERA AG": "Leica",
    "NIKON CORPORATION": "Nikon",
    "CANON": "Canon",
    "SONY": "Sony",
    "FUJIFILM": "Fujifilm",
    "OLYMPUS": "Olympus",
    "PANASONIC": "Panasonic",
    "RICOH IMAGING": "Ricoh",
}


def _clean_exif_make(make: str) -> str:
    """Normalise verbose EXIF make strings to friendly short names."""
    upper = make.upper().strip()
    for pattern, clean in _MAKE_NORMALISE.items():
        if upper == pattern or upper.startswith(pattern):
            return clean
    return make.strip()


def read_exif_camera(file_path: str) -> str | None:
    """
    Read EXIF Make + Model from a RAW still file using exifread.
    Returns "{Make} {Model}".strip() or None if unreadable.
    Only called when EXIF_AVAILABLE is True.
    """
    try:
        with open(file_path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="Image Model", details=False)
        make  = _clean_exif_make(str(tags.get("Image Make",  "")))
        model = str(tags.get("Image Model", "")).strip()
        # Drop redundant make prefix from model (e.g. "LEICA SL3" → "SL3" when make="Leica")
        if model.upper().startswith(make.upper()):
            model = model[len(make):].strip()
        result = f"{make} {model}".strip()
        return result if result else None
    except Exception:
        return None


def detect_xavc_brand(data: dict) -> str | None:
    """
    Detect Sony XAVC cameras from the MP4/MOV container brand fields.

    XAVC is Sony's proprietary codec used exclusively in FX3, FX6, FX9,
    Venice, ZV-E1 and Alpha series cameras.  The brand is embedded as
    major_brand or inside compatible_brands.  No model info is available
    so the bin name is always "Sony".
    """
    fmt      = data.get("format", {})
    fmt_tags = fmt.get("tags", {})

    major = fmt_tags.get("major_brand", "").strip()
    if major.upper() == "XAVC":
        return "Sony"

    brands = fmt_tags.get("compatible_brands", "") or fmt.get("compatible_brands", "")
    if "xavc" in brands.lower():
        return "Sony"

    return None


def detect_minor_version(data: dict, signatures: dict) -> str | None:
    """
    Check format.tags.minor_version against known container signatures.
    GoPro embeds decimal 538120216 (0x20199472) in every file it shoots.
    """
    minor = data.get("format", {}).get("tags", {}).get("minor_version", "").strip()
    return signatures.get(minor)


def detect_firmware(data: dict, firmware_sigs: dict) -> str | None:
    """
    Check format.tags.firmware for known camera firmware prefixes.
    GoPro firmware strings start with H followed by two digits (H21, H22 …).
    Matching checks whether the firmware value starts with the key (case-sensitive).
    """
    firmware = data.get("format", {}).get("tags", {}).get("firmware", "").strip()
    if not firmware:
        return None
    for prefix, camera in firmware_sigs.items():
        if firmware.startswith(prefix):
            return camera
    return None


def detect_brand_code(data: dict, brand_codes: dict) -> str | None:
    """
    Check format.compatible_brands for known camera brand identifiers.
    Matching is case-insensitive.  Returns the brand name or None.
    """
    fmt      = data.get("format", {})
    brands   = fmt.get("tags", {}).get("compatible_brands", "") or \
               fmt.get("compatible_brands", "")
    brands_l = brands.lower()
    for code, brand in brand_codes.items():
        if code.lower() in brands_l:
            return brand
    return None


def match_filename_pattern(filename: str, patterns: dict) -> str | None:
    """
    Check the filename stem against known prefix patterns.
    Longer patterns take priority over shorter ones so "GOPR" beats "G".
    Returns the full camera name or None.
    """
    stem = Path(filename).stem
    # Sort by descending prefix length so the most specific match wins
    for prefix in sorted(patterns, key=len, reverse=True):
        if stem.startswith(prefix):
            return patterns[prefix]
    return None


# ── Binary camera detection ───────────────────────────────────────────────────

# Cache: absolute_path → (mtime, (camera_name, camera_id) | None)
# camera_id is the serial for Sony cameras; None for all others.
_BINARY_CACHE: dict[str, tuple[float, tuple[str, str | None] | None]] = {}


def detect_camera_from_binary(file_path: str) -> tuple[str, str | None] | None:
    """
    Search the first 2 MB of a file's raw bytes for embedded camera model strings.

    Returns (camera_name, camera_id) or None.
    camera_id is:
      Sony ILME/ILCE — serial number embedded alongside model code in the binary
      all other cameras — None (model only)

    Reading only 2 MB keeps this fast (~1–2 ms per file on SSD).  Results are
    cached by (path, mtime) so re-scanning the same clip within a session is free.
    """
    global _BINARY_CACHE

    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return None

    cached = _BINARY_CACHE.get(file_path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(file_path, "rb") as f:
            data = f.read(2 * 1024 * 1024)
    except Exception:
        _BINARY_CACHE[file_path] = (mtime, None)
        return None

    result = _search_binary(data)
    _BINARY_CACHE[file_path] = (mtime, result)
    return result


def _search_binary(data: bytes) -> tuple[str, str | None] | None:
    """
    Apply all binary regex patterns to raw bytes.
    Returns (camera_name, camera_id_or_None) or None.
    camera_id is a serial string for Sony cameras; None for all others.
    """

    # ── Canon ─────────────────────────────────────────────────────────────────
    # Matches: "Canon EOS R5", "Canon EOS R6 Mark II", "Canon EOS C70" etc.
    # Two patterns: spaced ("EOS R5") and run-on ("EOSR5").
    m = re.search(rb'Canon\s+EOS\s+([A-Z][A-Z0-9](?:[A-Z0-9 \-]{0,12}?))'
                  rb'(?=[^A-Z0-9]|$)', data)
    if m:
        model = m.group(1).decode("ascii", errors="ignore").strip()
        return f"Canon EOS {model}", None

    m = re.search(rb'Canon\s+EOS([A-Z0-9]{1,6})', data)
    if m:
        return f"Canon EOS {m.group(1).decode('ascii', errors='ignore').strip()}", None

    # ── Sony ILME (Cinema/FX line: FX3, FX6, FX9, FX30) ─────────────────────
    # Serial is embedded in the same atom as the model: "ILME-FX3 5772936"
    m = re.search(rb'ILME-([A-Z0-9]{2,8})\s+(\d{5,10})', data)
    if m:
        model  = m.group(1).decode("ascii", errors="ignore").strip()
        serial = m.group(2).decode("ascii", errors="ignore").strip()
        return f"Sony {model}", serial

    m = re.search(rb'ILME-([A-Z0-9]{2,8})', data)
    if m:
        return f"Sony {m.group(1).decode('ascii', errors='ignore').strip()}", None

    # ── Sony ILCE (Alpha mirrorless: A7S III, A7R V, A7 IV) ──────────────────
    m = re.search(rb'ILCE-([A-Z0-9]{2,8})\s+(\d{5,10})', data)
    if m:
        model  = m.group(1).decode("ascii", errors="ignore").strip()
        serial = m.group(2).decode("ascii", errors="ignore").strip()
        return f"Sony Alpha {model}", serial

    m = re.search(rb'ILCE-([A-Z0-9]{2,8})', data)
    if m:
        return f"Sony Alpha {m.group(1).decode('ascii', errors='ignore').strip()}", None

    # ── Nikon ─────────────────────────────────────────────────────────────────
    # NEV files embed "NIKON CORPORATION\x00...\x10NIKON Z 8" — the model appears
    # as a second NIKON hit, not the first.  Use finditer to check every occurrence
    # and return the first that looks like a real model (excludes CORPORATION, RAW etc).
    _NIKON_EXCLUDE = {"CORPORATION", "RAW", "VIDEO", "MOVIE", "CODEC", "FORMAT", "VER"}
    for m in re.finditer(rb'NIKON\s+([A-Z0-9][A-Z0-9 ]{1,12}?)(?=[^A-Z0-9 ]|$)', data):
        model = m.group(1).decode("ascii", errors="ignore").strip()
        if not any(ex in model.upper() for ex in _NIKON_EXCLUDE) and 1 <= len(model) <= 12:
            return f"Nikon {model}", None

    # ── Leica ─────────────────────────────────────────────────────────────────
    # Files contain two LEICA occurrences: "LEICA CAMERA AG" (manufacturer)
    # followed by "LEICA SL3" (model).  Use finditer so "CAMERA AG" doesn't mask
    # the model occurrence that follows it.
    LEICA_MODELS  = ["SL3", "SL2-S", "SL2", "Q3", "Q2", "M11", "M10", "S3"]
    LEICA_EXCLUDE = {"CAMERA", "AG", "AG\x00"}
    for m in re.finditer(rb'LEICA\s+([A-Z0-9][A-Z0-9 \-]{1,10}?)(?=[^A-Z0-9 \-]|$)', data):
        model = m.group(1).decode("ascii", errors="ignore").strip()
        if any(ex in model for ex in LEICA_EXCLUDE):
            continue
        for known in LEICA_MODELS:
            if known in model:
                return f"Leica {known}", None
        if re.match(r'^[A-Z0-9][A-Z0-9 \-]{0,9}$', model) and len(model) <= 8:
            return f"Leica {model}", None

    # ── ARRI ──────────────────────────────────────────────────────────────────
    # Fallback for non-MXF ARRI files; MXF is already caught by company_name.
    m = re.search(rb'ALEXA\s+([A-Z][A-Z0-9 ]{1,12}?)(?=[^A-Z0-9 ]|$)', data)
    if m:
        model = m.group(1).decode("ascii", errors="ignore").strip()
        return f"ARRI ALEXA {model}", None

    # ── Panasonic ─────────────────────────────────────────────────────────────
    m = re.search(rb'Panasonic\s+([A-Z][A-Z0-9 \-]{1,15}?)(?=[^A-Z0-9 \-]|$)',
                  data, re.IGNORECASE)
    if m:
        model = m.group(1).decode("ascii", errors="ignore").strip()
        return f"Panasonic {model}", None

    # ── Fujifilm ──────────────────────────────────────────────────────────────
    m = re.search(rb'FUJIFILM\s+([A-Z][A-Z0-9 \-]{1,15}?)(?=[^A-Z0-9 \-]|$)',
                  data, re.IGNORECASE)
    if m:
        model = m.group(1).decode("ascii", errors="ignore").strip()
        return f"Fujifilm {model}", None

    # ── GoPro ─────────────────────────────────────────────────────────────────
    # HERO models: "HERO10 Black", "HERO12 Black", "HERO9 Black" etc.
    m = re.search(rb'HERO(\d+)\s+([A-Za-z]+)', data)
    if m:
        number  = m.group(1).decode("ascii").strip()
        variant = m.group(2).decode("ascii").strip()
        return f"GoPro HERO{number} {variant}", None

    # Non-HERO models: GoPro Max, GoPro 360 etc.
    m = re.search(rb'GoPro\s+([A-Z][A-Za-z0-9 ]{2,15}?)(?=[^A-Za-z0-9 ]|$)',
                  data, re.IGNORECASE)
    if m:
        model = m.group(1).decode("ascii", errors="ignore").strip()
        if len(model) >= 3 and "GPRO" not in model:
            return f"GoPro {model}", None

    return None


# ── Clip classification ───────────────────────────────────────────────────────

# Cache: absolute_path → (mtime, result_dict)
_CANON_EXIF_CACHE: dict[str, tuple[float, dict]] = {}


def extract_canon_exif(file_path: str) -> dict:
    """
    Extract Canon EXIF data from the CNDA block embedded in Canon MP4 files.
    Returns dict with keys: model, serial, lens, firmware.
    Returns empty dict if the CNDA block or embedded EXIF is not found.

    Canon MP4 files embed a JPEG EXIF block (FF D8 FF E1) inside a proprietary
    CNDA container atom.  The block contains human-readable strings for camera
    model, body serial (12 digits), firmware version, and lens model.  Reading
    10 MB covers all Canon metadata atoms regardless of clip length.

    Results are cached by (path, mtime) so re-scanning within a session is free.
    """
    global _CANON_EXIF_CACHE

    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return {}

    cached = _CANON_EXIF_CACHE.get(file_path)
    if cached and cached[0] == mtime:
        return cached[1]

    result: dict[str, str] = {}

    try:
        with open(file_path, "rb") as f:
            data = f.read(10 * 1024 * 1024)

        # Find CNDA container atom
        idx = data.find(b"CNDA")
        if idx >= 0:
            # Find embedded EXIF marker (FF D8 FF E1) after CNDA
            exif_start = data.find(b"\xff\xd8\xff\xe1", idx)
            if exif_start >= 0:
                # Extract printable ASCII strings from the 14 KB EXIF block
                chunk   = data[exif_start:exif_start + 14000]
                strings = re.findall(rb"[\x20-\x7e]{4,}", chunk)
                decoded = [s.decode("ascii", errors="ignore") for s in strings]

                for s in decoded:
                    if "model" not in result and re.match(r"Canon EOS [A-Z0-9]", s):
                        result["model"] = s.strip()
                    if "serial" not in result and re.match(r"^\d{12}$", s):
                        result["serial"] = s.strip()
                    if "firmware" not in result and s.startswith("Firmware Version"):
                        result["firmware"] = s.replace("Firmware Version", "").strip()
                    if "lens" not in result and re.match(r"^(RF|EF|EF-S|EF-M)\d", s):
                        result["lens"] = s.strip()

    except Exception:
        pass

    _CANON_EXIF_CACHE[file_path] = (mtime, result)
    return result


def extract_nikon_nctg(file_path: str) -> str | None:
    """
    Extract Nikon camera model from the NCTG (Nikon Container Tag Group) block
    embedded in Nikon video files (.MOV, .MP4).

    The NCTG atom contains manufacturer metadata including camera model and
    firmware version as printable strings.  The model may include a version
    suffix that must be stripped: "NIKON Z 8 Ver.01.00" → "Nikon Z 8".

    No serial number is embedded in Nikon video files.
    Returns "Nikon Z 8" style name, or None if NCTG block not found.
    """
    _NCTG_EXCLUDE = {"CORPORATION", "RAW", "VIDEO", "MOVIE"}

    try:
        with open(file_path, "rb") as f:
            data = f.read(2 * 1024 * 1024)

        idx = data.find(b"NCTG")
        if idx < 0:
            return None

        # Extract printable strings from 64 KB after the NCTG marker
        chunk   = data[idx : idx + 65536]
        strings = re.findall(rb"[\x20-\x7e]{4,}", chunk)

        for raw in strings:
            s = raw.decode("ascii", errors="ignore").strip()
            # Match "NIKON Z 8 Ver.01.00" or "NIKON Z 8"; capture model only
            m = re.match(
                r"^NIKON\s+([A-Z0-9][A-Z0-9 ]{1,14}?)(?:\s+Ver\.\d+\.\d+)?$", s
            )
            if not m:
                continue
            model = m.group(1).strip()
            if any(ex in model.upper() for ex in _NCTG_EXCLUDE):
                continue
            if 1 <= len(model) <= 15:
                return f"Nikon {model}"

        return None

    except Exception:
        return None


def format_panasonic_model(model_code: str) -> str:
    """
    Convert a Panasonic internal model code to a human-readable display name.

    DC-S1RM2  → "Panasonic S1R Mark II"
    DC-GH6    → "Panasonic GH6"
    DC-S5M2X  → "Panasonic S5 Mark IIX"
    DC-BS1H   → "Panasonic BS1H"
    DC-S9     → "Panasonic S9"

    Works for any future Panasonic model without requiring a lookup table update.
    """
    code = model_code.replace("DC-", "")

    def _mark(m: re.Match) -> str:
        n      = m.group(1)
        suffix = m.group(2)   # trailing letter e.g. "X" in M2X
        roman  = {"2": "II", "3": "III", "4": "IV", "5": "V"}.get(n, n)
        return f" Mark {roman}{suffix}"

    code = re.sub(r"M(\d)([A-Z]?)", _mark, code)
    return f"Panasonic {code.strip()}"


def detect_panasonic(file_path: str, ffprobe_data: dict) -> str | None:
    """
    Extract the exact Panasonic camera model from the XML document embedded in
    Panasonic MOV files.

    Triggered by "pana" in compatible_brands — all Panasonic Lumix cameras
    write this code.  The XML is stored in ffprobe format tag:
        format.tags["com.panasonic.Semi-Pro.metadata.xml"]
    and also directly embedded in the first 2 MB of the file binary.

    Preferred path: use the ffprobe tag string (already decoded, no I/O cost).
    Fallback: locate the XML via binary markers <?xml version … </ClipMain>.

    The XML uses a default namespace, so ElementTree lookups must use the
    namespace prefix (or wildcard search via ".//{*}ElementName").

    Maps model codes to friendly names via PANASONIC_MODELS; unknown codes
    fall back to stripping the "DC-" prefix from the code itself.

    Returns "Panasonic S1R Mark II" style name, or None on any failure.
    """
    xml_str: str | None = None

    # Path 1: ffprobe tag (no extra I/O)
    try:
        xml_str = ffprobe_data.get("format", {}).get("tags", {}).get(
            "com.panasonic.Semi-Pro.metadata.xml", ""
        ) or None
    except Exception:
        pass

    # Path 2: binary search (fallback if ffprobe tag absent)
    if not xml_str:
        try:
            with open(file_path, "rb") as f:
                data = f.read(2 * 1024 * 1024)
            xml_start = data.find(b"<?xml version")
            xml_end   = data.find(b"</ClipMain>")
            if xml_start >= 0 and xml_end >= 0:
                xml_str = data[xml_start : xml_end + len("</ClipMain>")].decode(
                    "utf-8", errors="replace"
                )
        except Exception:
            pass

    if not xml_str:
        return None

    try:
        root = ET.fromstring(xml_str)
        # Use wildcard namespace search: works regardless of xmlns declaration
        device = root.find(".//{*}Device")
        if device is None:
            return None
        model_code = (device.findtext("{*}ModelName") or "").strip()
        if not model_code:
            # ElementTree wildcard doesn't work for findtext in Python < 3.8;
            # fall back to iterating children directly
            for child in device:
                if child.tag.endswith("}ModelName") or child.tag == "ModelName":
                    model_code = (child.text or "").strip()
                    break
        if not model_code:
            return None
        return format_panasonic_model(model_code)
    except Exception:
        return None


def _detect_red_model(file_path: str) -> str:
    """
    Search the first 2 MB of an R3D file for a specific RED/Nikon camera model.

    Nikon ZR cameras (Nikon-owned RED hardware) are checked first.
    Falls back through specific RED sensor names before returning generic "RED".

    R3D files use the "niko" brand code (Nikon owns RED since 2024).
    Extension .r3d is detected before brand codes are reached, so there is no
    misclassification from the brand code.  Binary search here determines
    whether the camera is a Nikon ZR body or a traditional RED model.
    """
    try:
        with open(file_path, "rb") as f:
            data = f.read(2 * 1024 * 1024)

        # ── Nikon ZR (Nikon-branded RED bodies) ───────────────────────────────
        if re.search(rb"NIKON\s+ZR", data):
            return "Nikon ZR"
        # Broader Nikon match as fallback (should not appear in practice)
        _NIKON_EXCLUDE = {"CORPORATION", "RAW", "VIDEO", "MOVIE", "CODEC", "FORMAT"}
        for m in re.finditer(rb"NIKON\s+([A-Z0-9][A-Z0-9 ]{1,12}?)(?=[^A-Z0-9 ]|$)", data):
            model = m.group(1).decode("ascii", errors="ignore").strip()
            if not any(ex in model.upper() for ex in _NIKON_EXCLUDE) and 1 <= len(model) <= 12:
                return f"Nikon {model}"

        # ── RED models ────────────────────────────────────────────────────────
        RED_PATTERNS = [
            (rb"KOMODO-X",           "RED Komodo-X"),
            (rb"KOMODO\s*(\w+)",     None),           # "KOMODO 6K" → "RED Komodo 6K"
            (rb"V-RAPTOR\s*(\w*)",   None),           # "V-RAPTOR 8K" → "RED V-RAPTOR 8K"
            (rb"MONSTRO\s*(\w*)",    None),
            (rb"HELIUM\s*(\w*)",     None),
            (rb"GEMINI\s*(\w*)",     None),
            (rb"DRAGON-X\s*(\w*)",   None),
            (rb"DRAGON\s*(\w*)",     None),
        ]
        for pattern, fixed_name in RED_PATTERNS:
            m = re.search(pattern, data)
            if not m:
                continue
            if fixed_name:
                return fixed_name
            keyword = pattern.split(rb"\s")[0].decode("ascii")
            variant = m.group(1).decode("ascii", errors="ignore").strip() if m.lastindex else ""
            name = f"RED {keyword.title()}"
            if variant:
                name += f" {variant}"
            return name.strip()

    except Exception:
        pass

    return "RED"


def classify_clip(
    clip, use_ffprobe: bool, config: dict, debug: bool = False
) -> tuple[str, str, str, str | None]:
    """
    Returns (category, model_name, method, camera_id) where:
      category    — "Footage" | "Audio" | "Images" | "Graphics" |
                    "Timelines" | "Compound Clips"
      model_name  — camera model (bin name before multi-cam resolution)
      method      — human-readable string shown after "via" in progress output
      camera_id   — raw identifier for multi-cam separation:
                      BRAW:  camera letter "A"/"B"/"C" from camera_number tag
                      Canon: 12-digit body serial from CNDA EXIF block
                      Sony:  serial number embedded in binary ("ILME-FX3 5772936")
                      Nikon/Leica/GoPro/others: None (no serial available)
    """

    # Step 1: DaVinci Resolve clip-type property
    clip_type = (clip.GetClipProperty("Type") or "").strip()
    if clip_type == "Timeline":
        return "Timelines", "Timelines", "via type property", None
    if clip_type in ("Compound", "Compound Clip", "Fusion Clip"):
        return "Compound Clips", "Compound Clips", "via type property", None

    # Step 2: File path
    file_path_str = (clip.GetClipProperty("File Path") or "").strip()
    if not file_path_str:
        return "Footage", "Unknown", "no path", None

    filename = Path(file_path_str).name
    ext      = Path(file_path_str).suffix.lower()

    # Step 3: Extension-based fast detection
    if ext in BRAW_EXTS:
        if use_ffprobe:
            braw_data = run_ffprobe(file_path_str)
            if debug:
                print(f"    [DEBUG] ffprobe raw for {filename}:")
                print(f"    {json.dumps(braw_data, indent=2) if braw_data else 'no output'}")
            if braw_data:
                fmt_tags    = braw_data.get("format", {}).get("tags", {})
                camera_type = fmt_tags.get("camera_type", "").strip()
                if camera_type:
                    parts   = ["via camera_type tag"]
                    cam_num = fmt_tags.get("camera_number",    "").strip() or None
                    lens    = fmt_tags.get("lens_type",        "").strip()
                    fw      = fmt_tags.get("firmware_version", "").strip()
                    if lens:    parts.append(f"Lens: {lens}")
                    if cam_num: parts.append(f"Cam: {cam_num}")
                    if fw:      parts.append(f"FW: {fw}")
                    return "Footage", camera_type, " | ".join(parts), cam_num
        return "Footage", "Blackmagic RAW", "via extension", None
    if ext in RED_EXTS:
        model = _detect_red_model(file_path_str)
        method = "via binary search" if model != "RED" else "via extension"
        if debug and model != "RED":
            print(f"    [DEBUG] R3D binary hit for {filename}: {model!r}")
        return "Footage", model, method, None
    if ext in RED_SIDECAR_EXTS:
        # RED thumbnail/index sidecars — skip silently (no camera metadata)
        return "Footage", "Unknown", "RED sidecar", None
    if ext in CANON_EXTS:
        return "Footage", "Canon Cinema RAW", "via extension", None
    if ext in AUDIO_EXTS:
        return "Audio", "Audio", "via extension", None
    if ext in GRAPHIC_EXTS:
        return "Graphics", "Graphics", "via extension", None
    if ext in IMAGE_EXTS:
        return "Images", "Images", "via extension", None
    if ext in RAW_STILL_EXTS:
        if EXIF_AVAILABLE:
            camera = read_exif_camera(file_path_str)
            if camera:
                return "RAW Stills", f"{camera} RAW", "via EXIF", None
        return "RAW Stills", "RAW Stills", "via extension", None

    brand_codes       = config.get("brand_codes", {})
    filename_patterns = config.get("filename_patterns", {})
    minor_ver_sigs    = config.get("minor_version_signatures", {})
    firmware_sigs     = config.get("firmware_signatures", {})

    # Priority 1: Panasonic embedded XML — triggered by "pana" in compatible_brands.
    # Runs before binary search: Panasonic binary strings are unreliable (the
    # "Panasonic" pattern in _search_binary would match, but model code extraction
    # needs the full structured XML available in the container).
    if use_ffprobe:
        pana_probe = run_ffprobe(file_path_str)
        if pana_probe:
            fmt      = pana_probe.get("format", {})
            brands   = fmt.get("tags", {}).get("compatible_brands", "") or \
                       fmt.get("compatible_brands", "")
            if "pana" in brands.lower():
                pana_model = detect_panasonic(file_path_str, pana_probe)
                if pana_model:
                    if debug:
                        print(f"    [DEBUG] Panasonic XML hit for {filename}: {pana_model!r}")
                    return "Footage", pana_model, "via embedded XML", None

    # Priority 2: Canon CNDA EXIF block — model + 12-digit body serial
    canon_exif = extract_canon_exif(file_path_str)
    if debug and canon_exif:
        print(f"    [DEBUG] Canon EXIF for {filename}: {canon_exif}")
    if canon_exif.get("model"):
        camera = canon_exif["model"]
        serial = canon_exif.get("serial")
        lens   = canon_exif.get("lens")
        fw     = canon_exif.get("firmware")
        parts  = ["via embedded EXIF"]
        if serial: parts.append(f"SN:{serial}")
        if lens:   parts.append(f"Lens: {lens}")
        if fw:     parts.append(f"FW:{fw}")
        return "Footage", camera, " | ".join(parts), serial

    # Priority 4: Nikon NCTG block — model name with version suffix cleaned
    nikon_model = extract_nikon_nctg(file_path_str)
    if nikon_model:
        if debug:
            print(f"    [DEBUG] NCTG hit for {filename}: {nikon_model!r}")
        return "Footage", nikon_model, "via NCTG block", None

    # Priority 5: binary string search — universal camera pattern matching
    # Returns (camera_name, serial_or_None); Sony serial is embedded alongside
    # the model code in the file binary: "ILME-FX3 5772936".
    binary_result = detect_camera_from_binary(file_path_str)
    if binary_result:
        camera, binary_serial = binary_result
        if debug:
            serial_note = f" serial={binary_serial}" if binary_serial else ""
            print(f"    [DEBUG] binary hit for {filename}: {camera!r}{serial_note}")
        return "Footage", camera, "via binary search", binary_serial

    # Priority 6+: ffprobe-based detection
    ffprobe_data = None
    if use_ffprobe:
        ffprobe_data = run_ffprobe(file_path_str)
        if debug:
            print(f"    [DEBUG] ffprobe raw for {filename}:")
            print(f"    {json.dumps(ffprobe_data, indent=2) if ffprobe_data else 'no output'}")

        if ffprobe_data:
            # Priority 6: make/model tags (exact camera identity)
            camera = extract_camera_from_tags(ffprobe_data)
            if camera:
                return "Footage", camera, "via ffprobe tags", None

            # Priority 6b: company_name + product_name (MXF standard — ARRI, Sony Venice)
            result = extract_company_product(ffprobe_data)
            if result:
                camera, method = result
                return "Footage", camera, method, None

            # Priority 7: Android phone vendor tags
            result = extract_android_model(ffprobe_data)
            if result:
                camera, method = result
                return "Footage", camera, method, None

            # Priority 7b: minor_version container signature (reliable for GoPro)
            camera = detect_minor_version(ffprobe_data, minor_ver_sigs)
            if camera:
                return "Footage", camera, "via container signature", None

            # Priority 7c: firmware tag prefix (GoPro fallback)
            camera = detect_firmware(ffprobe_data, firmware_sigs)
            if camera:
                return "Footage", camera, "via firmware tag", None

            # Priority 8: XAVC brand → Sony (fallback if binary search found nothing)
            camera = detect_xavc_brand(ffprobe_data)
            if camera:
                return "Footage", camera, "via XAVC brand", None

            # Priority 8b: Leica brand code / L-Log tag
            result = detect_leica(ffprobe_data)
            if result:
                camera, method = result
                return "Footage", camera, method, None

            # Priority 9: compatible_brands code (CAEP → Canon, niko → Nikon, etc.)
            # Canon and Nikon are caught earlier by CNDA/NCTG if data is present;
            # this handles files where the embedded block is absent or unreadable.
            camera = detect_brand_code(ffprobe_data, brand_codes)
            if camera:
                return "Footage", camera, "via brand code", None

    # Priority 10: filename pattern — longest match wins (last resort)
    camera = match_filename_pattern(filename, filename_patterns)
    if camera:
        return "Footage", camera, "via filename", None

    # Fallbacks
    if ext in MXF_EXTS:
        return "Footage", "MXF", "via extension", None

    # DaVinci Resolve export detection — files with no camera metadata but a
    # Resolve encoder tag are exports, not originals from an unknown camera.
    if use_ffprobe and ffprobe_data is None:
        ffprobe_data = run_ffprobe(file_path_str)
    if ffprobe_data:
        encoder = (
            ffprobe_data.get("format", {}).get("tags", {}).get("encoder", "") or
            ffprobe_data.get("format", {}).get("tags", {}).get("com.apple.quicktime.software", "")
        ).lower()
        if "davinci resolve" in encoder or "blackmagic design" in encoder:
            return "Footage", "Exports", "via encoder tag", None

    return "Footage", "Unknown", "unknown", None


# ── Detection summary ─────────────────────────────────────────────────────────

def _print_detection_summary(classified: list) -> None:
    """
    Print a human-readable detection summary after classification.

    classified is the 7-tuple list produced before bin-name resolution:
    (clip, source_folder, source_path, category, model_name, method, camera_id)

    Footage cameras are listed first (most clips first, Unknown last), then
    non-footage categories.  Each line includes clip count and an annotation:
      BRAW        — [A Cam: 3 clips, B Cam: 3 clips]
      Canon/Sony  — [2 units: SN:xxx, SN:yyy] or [2 units via XML serial]
      Others      — [model only, serial not available]
      DJI/filename— [filename detection only]
    """
    # ── Collect Footage stats per model ──────────────────────────────────────
    model_stats: dict[str, dict] = {}
    for _, _, _, cat, model, method, cid in classified:
        if cat != "Footage":
            continue
        if model not in model_stats:
            model_stats[model] = {"count": 0, "ids": [], "methods": []}
        model_stats[model]["count"]     += 1
        model_stats[model]["ids"].append(cid)
        model_stats[model]["methods"].append(method)

    # ── Collect non-Footage stats per category ────────────────────────────────
    # category → {count, sub: {model → count}}
    cat_stats: dict[str, dict] = {}
    for _, _, _, cat, model, method, cid in classified:
        if cat == "Footage":
            continue
        if cat not in cat_stats:
            cat_stats[cat] = {"count": 0, "sub": defaultdict(int)}
        cat_stats[cat]["count"]    += 1
        cat_stats[cat]["sub"][model] += 1

    # ── Column width ──────────────────────────────────────────────────────────
    all_names  = list(model_stats.keys()) + list(cat_stats.keys())
    name_width = min(max((len(n) for n in all_names), default=8) + 2, 28)

    print("\nCamera detection complete:")

    # ── Footage cameras — most clips first, Unknown last ──────────────────────
    sorted_footage = sorted(
        model_stats.items(),
        key=lambda x: (x[0] == "Unknown", -x[1]["count"], x[0]),
    )

    for model_name, info in sorted_footage:
        count   = info["count"]
        ids     = info["ids"]
        methods = info["methods"]
        non_none = [cid for cid in ids if cid is not None]
        unique   = list(dict.fromkeys(non_none))    # dedupe, preserve order
        clip_s   = "clip" if count == 1 else "clips"
        line     = f"  {model_name:<{name_width}} — {count} {clip_s}"

        if non_none:
            if all(len(cid) == 1 and cid in "ABCDEF" for cid in non_none):
                # BRAW camera letters — show per-letter clip counts
                letter_counts = Counter(non_none)
                parts = [
                    f"{k} Cam: {v} clip{'s' if v > 1 else ''}"
                    for k, v in sorted(letter_counts.items())
                ]
                line += f"  [{', '.join(parts)}]"
            else:
                # Serial-based (Canon CNDA, Sony binary) — show actual SN values
                n = len(unique)
                sn_list = ", ".join(f"SN:{s}" for s in unique[:4])
                if len(unique) > 4:
                    sn_list += f" +{len(unique) - 4} more"
                line += f"  [{n} unit{'s' if n > 1 else ''}: {sn_list}]"
        elif model_name != "Unknown":
            if any("via filename" in m for m in methods):
                line += "  [filename detection only]"
            else:
                line += "  [model only, serial not available]"

        print(line)

    # ── Non-footage categories ─────────────────────────────────────────────────
    cat_order = ["Audio", "Images", "RAW Stills", "Timelines",
                 "Compound Clips", "Graphics"]
    ordered = [c for c in cat_order if c in cat_stats]
    ordered += sorted(c for c in cat_stats if c not in cat_order)

    for cat in ordered:
        info   = cat_stats[cat]
        count  = info["count"]
        clip_s = "clip" if count == 1 else "clips"
        line   = f"  {cat:<{name_width}} — {count} {clip_s}"

        # RAW Stills: show per-camera breakdown when multiple bins are present
        if cat == "RAW Stills":
            named = {k: v for k, v in info["sub"].items() if k != "RAW Stills"}
            if named:
                parts = [f"{m}: {n}" for m, n in sorted(named.items(), key=lambda x: -x[1])]
                if info["sub"].get("RAW Stills"):
                    parts.append(f"other: {info['sub']['RAW Stills']}")
                line += f"  [{', '.join(parts)}]"

        print(line)


# ── Media Pool traversal ──────────────────────────────────────────────────────

def collect_clips(folder, _path: str = "") -> list[tuple]:
    """
    Recursively walk all bins.
    Returns list of (clip, source_folder, source_path) where source_path is
    the full slash-joined folder path from root, e.g. "Master/Footage/Canon EOS R5".
    This path is used later to determine whether a clip is already in the
    correct destination bin — a name-only comparison would give false matches
    if the same bin name exists at different levels.
    """
    folder_name  = (folder.GetName() or "").strip()
    current_path = f"{_path}/{folder_name}" if _path else folder_name
    results = []
    for clip in folder.GetClipList() or []:
        results.append((clip, folder, current_path))
    for subfolder in folder.GetSubFolderList() or []:
        results.extend(collect_clips(subfolder, current_path))
    return results


# ── Bin helpers ───────────────────────────────────────────────────────────────

def find_bin(parent_folder, name: str):
    """Return the direct sub-folder with the given name, or None."""
    for subfolder in parent_folder.GetSubFolderList() or []:
        if subfolder.GetName() == name:
            return subfolder
    return None


def get_or_create_bin(
    media_pool, parent_folder, name: str, dry_run: bool, created_set: set
) -> tuple:
    """
    Return (folder_or_None, was_created).

    Normal mode: creates the bin if it doesn't exist; returns the folder object.
    Dry-run mode: never calls AddSubFolder; returns (None, True) if the bin
                  would be created, or (existing_folder, False) if it exists.
    """
    existing = find_bin(parent_folder, name) if parent_folder is not None else None
    if existing is not None:
        return existing, False

    if dry_run:
        created_set.add(name)
        return None, True

    new_folder = media_pool.AddSubFolder(parent_folder, name)
    if new_folder:
        created_set.add(name)
        return new_folder, True

    print(f"  Warning: Failed to create bin '{name}'")
    return None, False


# ── Main orchestration ────────────────────────────────────────────────────────

def main():
    # ── Startup banner ────────────────────────────────────────────────────────
    print("═══════════════════════════════════════")
    print(f"  Sort Media Pool  v{VERSION}")
    print("  by DrRave  —  drrave.com")
    print("═══════════════════════════════════════")

    parser = argparse.ArgumentParser(
        description="Organise DaVinci Resolve Media Pool by camera and media type."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would happen without making any changes.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print raw ffprobe JSON for every clip to diagnose camera detection.",
    )
    args = parser.parse_args()
    dry_run: bool = args.dry_run
    debug: bool   = args.debug

    if dry_run:
        print("[DRY RUN] No changes will be made.\n")

    # ── Connect to Resolve ────────────────────────────────────────────────────
    try:
        resolve = get_resolve()
    except Exception as e:
        print(f"Error: Could not connect to DaVinci Resolve — {e}")
        print("Make sure Resolve is open with a project loaded, then try again.")
        sys.exit(1)
    if resolve is None:
        print("Error: Could not connect to DaVinci Resolve.")
        print("Make sure Resolve is open with a project loaded, then try again.")
        sys.exit(1)

    try:
        fusion = resolve.Fusion()
    except Exception:
        fusion = None
    check_for_updates(VERSION, resolve=resolve, fusion=fusion)

    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        print("Error: No project is currently open in DaVinci Resolve.")
        sys.exit(1)

    media_pool  = project.GetMediaPool()
    root_folder = media_pool.GetRootFolder()

    # ── Check exifread ────────────────────────────────────────────────────────
    if not EXIF_AVAILABLE:
        print(
            "Warning: exifread not installed — RAW stills (.dng, .cr2, .nef etc) "
            "will go to a generic 'RAW Stills' bin.\n"
            "  Install with:  pip3 install exifread\n"
        )

    # ── Load config ───────────────────────────────────────────────────────────
    config = load_config()

    # ── Check ffprobe ─────────────────────────────────────────────────────────
    use_ffprobe = check_ffprobe()
    if not use_ffprobe:
        print("⚠ ffprobe not found in DrRave/ffmpeg/ folder")
        print("  Camera detection will use file extension and filename only.")
        print("  For full camera detection, add ffprobe to:")
        print("    Mac: DrRave/ffmpeg/mac/ffprobe")
        print("    Win: DrRave/ffmpeg/win/ffprobe.exe")
        print()

    # ── Scan ──────────────────────────────────────────────────────────────────
    print("Scanning Media Pool...")
    all_clips = collect_clips(root_folder)

    if not all_clips:
        print("No clips found in Media Pool.")
        sys.exit(0)

    total = len(all_clips)
    source_bin_count = len({id(f) for _, f, _ in all_clips})
    print(f"Found {total} clips across {source_bin_count} bin(s)\n")

    # ── Classify every clip ───────────────────────────────────────────────────
    print("Detecting cameras...")
    # (clip, source_folder, source_path, category, model_name, method, camera_id)
    classified = []

    for i, (clip, source_folder, source_path) in enumerate(all_clips, start=1):
        clip_name = clip.GetName() or "unnamed"
        category, model_name, method, camera_id = classify_clip(
            clip, use_ffprobe, config, debug
        )

        method_note = f" ({method})" if not method.startswith("unknown") else ""
        print(f"  [{i}/{total}] {clip_name} → {model_name}{method_note}")

        classified.append(
            (clip, source_folder, source_path, category, model_name, method, camera_id)
        )

    # ── Detection summary + multi-camera bin resolution ──────────────────────
    # camera_id sources:
    #   BRAW  — camera letter from camera_number tag ("A", "B", "C" …)
    #           always suffixed regardless of unit count (user-set production ID)
    #   Canon — 12-digit serial from CNDA EXIF block
    #   Sony  — serial embedded in binary alongside model: "ILME-FX3 5772936"
    #   Nikon/Leica/GoPro/others — None (no serial available)
    #
    # RULE: serial-based cameras split bins only when 2+ DIFFERENT serials are
    # found across all clips.  Single-serial or no-serial → one clean bin.

    # Build per-model lists of camera_ids (Footage only)
    model_ids: dict[str, list[str | None]] = defaultdict(list)
    for _, _, _, cat, model_name, _, camera_id in classified:
        if cat == "Footage":
            model_ids[model_name].append(camera_id)

    # For each model, decide bin assignment
    # Returns: final_bin_name for (model_name, camera_id)
    def _resolve_bin(model_name: str, camera_id: str | None) -> str:
        ids      = model_ids[model_name]
        non_none = [cid for cid in ids if cid is not None]
        unique   = set(non_none)

        if len(unique) <= 1:
            # Single camera unit — include letter suffix for BRAW, drop for serials
            if camera_id in ("A", "B", "C", "D", "E", "F"):
                return f"{model_name} - {camera_id} Cam"
            return model_name

        # Multiple camera units — split bins
        if camera_id in ("A", "B", "C", "D", "E", "F"):
            return f"{model_name} - {camera_id} Cam"

        if camera_id is not None:
            # Serial-based (Canon CNDA, Sony XAVC): assign Cam 1 / Cam 2 … by first appearance
            serial_map: dict[str, str] = {}
            for cid in ids:
                if cid is not None and cid not in serial_map:
                    serial_map[cid] = f"Cam {len(serial_map) + 1}"
            return f"{model_name} - {serial_map[camera_id]}"

        # No ID for this clip but others of the same model have IDs
        return model_name

    # Print full detection summary (before bin resolution so model_name is still raw)
    _print_detection_summary(classified)

    # Build final classified list with resolved bin_name
    # (clip, source_folder, source_path, category, bin_name, method)
    classified_resolved = []
    for clip, source_folder, source_path, category, model_name, method, camera_id in classified:
        if category == "Footage":
            bin_name = _resolve_bin(model_name, camera_id)
        elif category == "RAW Stills":
            bin_name = model_name
        else:
            bin_name = model_name
        classified_resolved.append(
            (clip, source_folder, source_path, category, bin_name, method)
        )
    classified = classified_resolved

    # ── Determine which bins are needed ───────────────────────────────────────
    needs_footage    = any(c == "Footage"        for _, _, _, c, _, _ in classified)
    needs_audio      = any(c == "Audio"          for _, _, _, c, _, _ in classified)
    needs_images     = any(c == "Images"         for _, _, _, c, _, _ in classified)
    needs_timelines  = any(c == "Timelines"      for _, _, _, c, _, _ in classified)
    needs_compound   = any(c == "Compound Clips" for _, _, _, c, _, _ in classified)
    needs_graphics   = any(c == "Graphics"       for _, _, _, c, _, _ in classified)
    needs_raw_stills = any(c == "RAW Stills"     for _, _, _, c, _, _ in classified)

    unique_cameras   = sorted({bn for _, _, _, cat, bn, _ in classified if cat == "Footage"})
    unique_raw_bins  = sorted({bn for _, _, _, cat, bn, _ in classified if cat == "RAW Stills"})

    # ── Create bin structure ──────────────────────────────────────────────────
    print("\nCreating bin structure...")
    created_bins: set[str] = set()

    # Resolve's root folder is named "Master" by default. If it is, use it
    # directly — creating a "Master" bin inside it would give Master → Master.
    if (root_folder.GetName() or "").strip() == "Master":
        master_folder = root_folder
        master_parent = root_folder
    else:
        master_folder, master_new = get_or_create_bin(
            media_pool, root_folder, "Master", dry_run, created_bins
        )
        if master_new:
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"  {prefix}Create bin: Master")
        # In dry-run, master_folder is None when Master doesn't exist yet.
        # Use root_folder as a proxy so subsequent find_bin calls still work.
        master_parent = master_folder if master_folder is not None else root_folder

    # Footage + camera sub-bins
    footage_folder = None
    camera_folders: dict[str, object] = {}

    if needs_footage:
        footage_folder, footage_new = get_or_create_bin(
            media_pool, master_parent, "Footage", dry_run, created_bins
        )
        if footage_new:
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"  {prefix}Create bin: Footage (under Master)")

        footage_parent = footage_folder if footage_folder is not None else root_folder

        for cam in unique_cameras:
            cam_folder, cam_new = get_or_create_bin(
                media_pool, footage_parent, cam, dry_run, created_bins
            )
            camera_folders[cam] = cam_folder
            if cam_new:
                prefix = "[DRY RUN] " if dry_run else ""
                print(f"  {prefix}Create bin: {cam} (under Master/Footage)")

    # Top-level media-type bins under Master
    type_folders: dict[str, object] = {}

    type_bin_specs = [
        (needs_audio,     "Audio"),
        (needs_images,    "Images"),
        (needs_timelines, "Timelines"),
        (needs_compound,  "Compound Clips"),
        (needs_graphics,  "Graphics"),
    ]
    for needed, name in type_bin_specs:
        if not needed:
            continue
        folder, is_new = get_or_create_bin(
            media_pool, master_parent, name, dry_run, created_bins
        )
        type_folders[name] = folder
        if is_new:
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"  {prefix}Create bin: {name} (under Master)")

    # RAW Stills bin + per-camera sub-bins
    raw_stills_folders: dict[str, object] = {}   # bin_name → folder

    if needs_raw_stills:
        raw_parent_folder, raw_new = get_or_create_bin(
            media_pool, master_parent, "RAW Stills", dry_run, created_bins
        )
        type_folders["RAW Stills"] = raw_parent_folder
        if raw_new:
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"  {prefix}Create bin: RAW Stills (under Master)")

        raw_folder_parent = raw_parent_folder if raw_parent_folder is not None else root_folder

        for raw_bin in unique_raw_bins:
            if raw_bin == "RAW Stills":
                # Generic fallback — clips go directly into the RAW Stills parent
                raw_stills_folders[raw_bin] = raw_parent_folder
            else:
                sub_folder, sub_new = get_or_create_bin(
                    media_pool, raw_folder_parent, raw_bin, dry_run, created_bins
                )
                raw_stills_folders[raw_bin] = sub_folder
                if sub_new:
                    prefix = "[DRY RUN] " if dry_run else ""
                    print(f"  {prefix}Create bin: {raw_bin} (under Master/RAW Stills)")

    # ── Build destination path map ────────────────────────────────────────────
    # Map each bin_name to its full path so we can compare against source_path.
    # Paths are built to match the format produced by collect_clips(), e.g.
    # "Master/Footage/Canon EOS R5".  In dry-run the folder objects may be None
    # (bins not yet created); we still compute the expected path for reporting.
    master_name = (root_folder.GetName() or "Master").strip()

    dest_paths: dict[str, str] = {}

    for cam in unique_cameras:
        dest_paths[cam] = f"{master_name}/Footage/{cam}"

    for name in ("Audio", "Images", "Timelines", "Compound Clips", "Graphics"):
        dest_paths[name] = f"{master_name}/{name}"

    for raw_bin in unique_raw_bins:
        if raw_bin == "RAW Stills":
            dest_paths[raw_bin] = f"{master_name}/RAW Stills"
        else:
            dest_paths[raw_bin] = f"{master_name}/RAW Stills/{raw_bin}"

    # ── Resolve destination for each clip ─────────────────────────────────────
    print("\nMoving clips...")

    moved_count      = 0
    already_ok_count = 0
    skipped_count    = 0

    # Group clips by their destination bin name
    groups: dict[str, list] = defaultdict(list)

    for clip, source_folder, source_path, category, bin_name, method in classified:
        if category == "Footage":
            dest = camera_folders.get(bin_name)
        elif category == "RAW Stills":
            dest = raw_stills_folders.get(bin_name)
        else:
            dest = type_folders.get(category)

        # Skip clips already in the correct bin — compare full paths so a bin
        # named "Audio" somewhere outside the Master structure is not confused
        # with Master/Audio.
        expected_path = dest_paths.get(bin_name, "")
        if source_path == expected_path:
            already_ok_count += 1
            continue

        groups[bin_name].append((clip, source_folder, dest))

    # ── Move / report ─────────────────────────────────────────────────────────
    for bin_name, items in groups.items():
        clip_objects = [clip for clip, _, _ in items]
        dest         = items[0][2]

        if dry_run:
            print(f"  [DRY RUN] Would move {len(clip_objects)} clip(s) → {bin_name}")
            moved_count += len(clip_objects)
            continue

        if dest is None:
            print(
                f"  Warning: No destination folder for '{bin_name}' — "
                f"skipping {len(clip_objects)} clip(s)"
            )
            skipped_count += len(clip_objects)
            continue

        ok = media_pool.MoveClips(clip_objects, dest)
        if ok:
            moved_count += len(clip_objects)
        else:
            # Retry one-by-one to isolate the failing clip
            for clip, _, _ in items:
                ok_single = media_pool.MoveClips([clip], dest)
                if ok_single:
                    moved_count += 1
                else:
                    print(f"  Warning: Could not move '{clip.GetName()}' → {bin_name}")
                    skipped_count += 1

    # ── Per-bin summary ───────────────────────────────────────────────────────
    print()
    for bin_name, items in sorted(groups.items()):
        print(f"  {bin_name}: {len(items)} clip(s)")

    if already_ok_count:
        print(f"  (already in correct bin: {already_ok_count})")

    # ── Final summary ─────────────────────────────────────────────────────────
    total_processed = moved_count + already_ok_count + skipped_count

    print()
    print("═" * 35)
    print("Media Pool organised successfully")
    print(f"Total clips processed:        {total_processed}")
    print(f"Bins created:                 {len(created_bins)}")
    print(f"Clips moved:                  {moved_count}")
    print(f"Clips already in correct bin: {already_ok_count}")
    print(f"Clips skipped (errors):       {skipped_count}")
    print("═" * 35)


if __name__ == "__main__":
    main()
