# Agent Guidelines & Development Standards

## 1. Project Overview & Domain Context

**`rekordbox-compatibility-converter`** is a high-performance, cross-platform CLI and modern GUI utility designed for DJs.

### The Problem
When DJs export tracks to a USB drive from Rekordbox, many club-standard CDJs (e.g., **CDJ-2000NXS**, **CDJ-900NXS**, **XDJ-1000 MK1**, **XDJ-700**, **XDJ-RX**, and **CDJ-2000 original**) do not support **FLAC**, **ALAC**, or sample rates above **48 kHz**. Loading these files results in `E-8302: CANNOT PLAY TRACK`.

### The Solution
This tool points directly at an exported Rekordbox USB drive and:
1. Converts audio files in parallel via FFmpeg to **AIFF** (default, bit-identical PCM timing ensuring **zero beatgrid drift**), WAV, or MP3.
2. Synchronizes Pioneer's proprietary binary database (**`PIONEER/rekordbox/export.pdb`** - DeviceSQL format).
3. Synchronizes Pioneer's binary analysis files (**`PIONEER/USBANLZ/.../ANLZxxxx.DAT`, `.EXT` & `.2EX`** - PMAI/PPTH tags) so waveforms, beatgrids, and hot cues load instantly without re-analysis.
4. Cleans macOS hidden AppleDouble (`._*`) and `.DS_Store` ghost files that cause CDJs to freeze or report read errors.
5. Provides space-saving conversion by hash-verifying originals in a user-selected local recovery archive before removing them from the USB; the final converted library must still fit.

---

## 2. Environment & Tooling Standards

- **Python Package Management**: Always use `uv` exclusively:
  - Virtual environment creation: `uv venv`
  - Package installation: `uv pip install ...` or `uv add ...`
  - Running scripts, CLI, or tests: `uv run --extra dev pytest -v`, `uv run rbconvert ...`, `uv run rbconvert-gui`
- **Python Version**: Support Python 3.9+. Use modern typing (`typing.Optional`, `typing.List`, `typing.Dict`, `typing.Tuple`).
- **External Dependencies**:
  - `click` & `rich` (CLI)
  - `customtkinter` & `darkdetect` (Modern GUI)
  - `ffmpeg` & `ffprobe` (System tools for source installs; minimal LGPL builds are bundled in desktop releases)

---

## 3. Architecture & Codebase Map

```
rekordbox_compatibility_converter/
├── core/
│   ├── models.py           # Dataclasses (TrackInfo, TargetFormat, ConversionTask, ScanSummary)
│   ├── profiles.py         # CDJ compatibility profiles (standard, maximum, modern)
│   ├── pdb_manager.py      # Binary DeviceSQL export.pdb parser and in-place heap patcher
│   ├── anlz_manager.py     # Binary PMAI / PPTH ANLZ parser and header synchronizer
│   ├── dlp_manager.py      # DeviceLibraryPlus (exportLibrary.db) manager
│   ├── audio_converter.py  # Multi-format FFmpeg converter with TPDF dithering
│   ├── local_backup.py     # Verified off-USB original/metadata archive and guarded restore
│   ├── usb_detector.py     # Cross-platform USB mount point detector
│   ├── validator.py        # End-to-end database, audio, and ANLZ integrity checker
│   └── engine.py           # Multi-threaded orchestrator, backup/restore, dotfile cleaner
├── cli/
│   └── main.py             # Rich CLI (scan, convert, verify, restore, drives, profiles)
├── gui/
│   ├── app.py              # GUI entrypoint with graceful fallback
│   └── modern_app.py       # CustomTkinter modern dark/light UI
└── tests/                  # Pytest unit and integration test suite
```

---

## 4. Pioneer Binary Specifications & Quirks

### `export.pdb` (DeviceSQL Table 0: `tracks`)
- **Page Size**: 4096 bytes (`len_page = 4096`).
- **Header**: 40 bytes (`0x28`). Heap data starts at `0x28`.
- **Page flags at `0x18`**: `raw18 = struct.unpack_from("<I", page_data, 0x18)[0]`.
  - `num_row_offsets = raw18 & 0x1FFF`
  - `num_rows = (raw18 >> 13) & 0x7FF`
  - `page_flags = (raw18 >> 24) & 0xFF` (`(page_flags & 0x40) == 0` indicates a track data page).
- **Row groups**: Stack backwards from `len_page - (group_index * 0x24)`.
- **Track Row Field Offsets**:
  - `+0x08`: `sample_rate` (`u4` little-endian)
  - `+0x10`: `file_size` (`u4` little-endian)
  - `+0x30`: `bitrate` (`u4` little-endian)
  - `+0x48`: `track_id` (`u4` little-endian)
  - `+0x52`: `sample_depth` (`u2` little-endian)
  - `+0x5e`: String offset array (`21 * u2`).
    - Index `14`: `analyze_path` (e.g. `/PIONEER/USBANLZ/.../ANLZ0000.DAT`)
    - Index `19`: `filename` (e.g. `Song.aiff`)
    - Index `20`: `file_path` (e.g. `/Contents/Artist/Album/Song.aiff`)
- **In-Place Patching Strategy**: Replacing `.flac` with `.aiff` uses identical 5-character string lengths (`.flac` = 5 bytes, `.aiff` = 5 bytes), allowing zero-offset-shift in-place byte patching.

### `ANLZ` Files (`.DAT`, `.EXT`, and `.2EX`)
- **Format**: Big-endian tagged binary structure starting with magic header `PMAI` (28 bytes).
- **Section Tag `PPTH`**: Contains the relative file path encoded in **UTF-16BE**.
- **Waveform generations**: `.DAT` stores legacy waveform data, `.EXT` stores RGB waveform data, and `.2EX` stores newer 3-band waveform data. Existing sidecars must remain path-consistent as a set.
- **Header Sync**: If path length changes, both the `PPTH` section length and `len_file` at offset `0x08` in the `PMAI` header must be recalculated. Offset `0x04` stores `len_header`.
- **No PPTH alignment padding**: The PPTH body is exactly the NUL-terminated UTF-16BE path. Do not insert four-byte alignment padding before the following tag.

---

## 5. UI/UX & Design Standards

- **Visual Tone**: Professional, sleek, and minimalist (matching modern DJ software like Rekordbox, Traktor, Ableton).
- **No Emojis**: Do not use casual emojis in button labels or headers. Use clean text labels and standard typography.
- **Copywriting**:
  - Use clear, DJ-centric terminology (e.g., `CDJ-2000NXS`, `Sample Rate`, `Bit Depth`, `Waveforms & Beatgrids`).
  - **NEVER** expose internal development slang or conversation references (e.g., do not write "Option A").
  - Always explain hardware profile targets with dynamic helper descriptions.
- **Contrast & Dynamic Theming**:
  - All stat cards, labels, and table views must have verified WCAG contrast in both **Dark** and **Light** modes.
  - `ttk.Treeview` tables inside CustomTkinter frames must be dynamically styled to match the active appearance mode.
- **Defensive Layout**:
  - Always pin bottom progress bars and status labels (`side="bottom"`) so that expanding tables never clip them.

---

## 6. Git & Commit Guidelines

- **Author Identity**: Commits in this repository must always use:
  - `user.name = "Resonant Circuits"`
  - `user.email = "resonantcircuits@pm.me"`
- **Push Policy**: **NEVER push to remote without explicit user confirmation.** Always ask or wait for the user to review local changes first.
- **Release Tags**: Before creating or moving a release tag, update the version in
  `pyproject.toml`, refresh `uv.lock` with `uv lock`, commit both files, and run the
  same tag/version assertion used by CI. Never create or push `vX.Y.Z` unless the
  committed package version is exactly `X.Y.Z`.

---

## 7. Quality Assurance & Testing

Before concluding any work, run the test suite:
```bash
uv run --extra dev pytest -v
```
All tests must pass. When adding new features, add corresponding unit tests in `tests/`.
