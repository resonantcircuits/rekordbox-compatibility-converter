# Rekordbox Format Checker & CDJ Compatibility Converter

[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](tests/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A high-speed, cross-platform CLI and modern Dark-Mode GUI utility designed for DJs that converts exported Rekordbox USB libraries into CDJ/XDJ-compatible audio formats (e.g. FLAC $\rightarrow$ AIFF/WAV/MP3) while **automatically synchronizing the `export.pdb` database, `ANLZ` analysis files (beatgrids, cues, waveforms), and `DeviceLibraryPlus`**.

---

## The Problem

When DJs export tracks to a USB drive from Rekordbox, many legacy and standard club CDJs (such as the industry-standard **CDJ-2000NXS**, **CDJ-900NXS**, **XDJ-1000 MK1**, **XDJ-700**, **XDJ-RX**, and **CDJ-2000 original**) do not support **FLAC**, **ALAC**, or sample rates above **48 kHz**. 

When loading a FLAC track on these players, the player throws:
> `E-8302: CANNOT PLAY TRACK` or `UNSUPPORTED FILE FORMAT`

Rekordbox does not provide an "Export in Compatible Format" option. Manually converting files on a computer breaks Rekordbox track paths, waveforms, beatgrids, and hot cues.

## The Solution

This tool points directly at an **already-exported Rekordbox USB drive** and:
1. **Converts incompatible audio in parallel** (FLAC/ALAC $\rightarrow$ 16/24-bit 44.1/48kHz AIFF or WAV) using multi-threaded FFmpeg with audiophile TPDF dithering and ID3 artwork preservation.
2. **Maintains sample-accurate beatgrids & cues**: Because lossless AIFF is uncompressed PCM with bit-identical sample timing, Rekordbox beatgrids and hot cues remain 100% accurate with **zero grid drift**.
3. **Patches the binary `export.pdb` database**: Updates track filenames, file paths, file sizes, sample rates, bit depths, and bitrates directly in the Pioneer `DeviceSQL` database.
4. **Patches `ANLZ` analysis files (`.DAT` and `.EXT`)**: Updates internal `PPTH` file path tags so waveforms and beatgrids load seamlessly on CDJs.
5. **Space-Saving In-Place Conversion (Option A)**: Automatically frees USB disk space by deleting each original FLAC only after successful conversion and database synchronization.
6. **Cleans macOS Ghost Files**: Automatically removes hidden AppleDouble (`._*`) and `.DS_Store` files that can cause CDJs to freeze or display read errors.
7. **One-Click Backup & Rollback**: Creates automatic `.bak` backups and provides an instant `restore` command.

---

## Compatibility Profiles

| Profile | Target Hardware | Conversion Rules | Default Target |
| :--- | :--- | :--- | :--- |
| **`standard`** *(Default)* | CDJ-2000NXS, CDJ-900NXS, XDJ-1000, XDJ-700, XDJ-RX/RX2 | • Convert FLAC/ALAC $\rightarrow$ AIFF<br>• Downsample $>48\text{ kHz}$ to 44.1/48 kHz<br>• Keep MP3, AAC, and valid WAV/AIFF intact | **AIFF** (16/24-bit) |
| **`maximum`** *(Legacy)* | CDJ-2000 orig, CDJ-850, CDJ-350, XDJ-AERO | • Enforce strict 16-bit 44.1 kHz AIFF/WAV/MP3 for vintage players | **AIFF** (16-bit 44.1 kHz) |
| **`modern`** *(Validation)* | CDJ-3000, CDJ-2000NXS2, XDJ-XZ, XDJ-RX3, OPUS-QUAD | • Allows FLAC, ALAC, WAV up to 24-bit 96 kHz.<br>• Validates database integrity | **AIFF** |

---

## Installation

### Requirements
- **Python 3.9+**
- **FFmpeg** installed on your system (`brew install ffmpeg` on macOS, `choco install ffmpeg` on Windows, or `apt install ffmpeg` on Linux).

### Using `uv` (Recommended)
```bash
git clone https://github.com/your-username/rekordbox-compatibility-converter.git
cd rekordbox-compatibility-converter
uv venv
uv pip install -e ".[dev]"
```

### Using standard `pip`
```bash
pip install -e .
```

---

## Usage

### 1. Modern Dark-Mode GUI

Launch the visual application:
```bash
rbconvert-gui
```
- Select your USB drive from the auto-populated dropdown.
- Choose your desired CDJ profile and target format.
- Adjust parallel worker threads to match your CPU cores.
- Click **"Scan USB Drive"** to view a live breakdown of your library and incompatible tracks.
- Click **"Convert Incompatible Tracks"** to start the parallel conversion with real-time progress tracking.

---

### 2. Command-Line Interface (CLI)

#### Auto-Detect & Scan USB Drive (Dry Run)
```bash
rbconvert scan
# Or specify mount path explicitly:
rbconvert scan /Volumes/YOUR_USB
```

#### Convert Incompatible Tracks (Multi-Threaded)
```bash
# Interactive conversion with confirmation prompt
rbconvert convert /Volumes/YOUR_USB

# Non-interactive immediate conversion with 8 parallel worker threads
rbconvert convert /Volumes/YOUR_USB -y --threads 8

# Specify target profile (e.g. legacy CDJ-350/850)
rbconvert convert /Volumes/YOUR_USB --profile maximum -y

# Convert to 320kbps MP3 or WAV instead of AIFF
rbconvert convert /Volumes/YOUR_USB --format mp3 -y

# Safely eject USB after conversion
rbconvert convert /Volumes/YOUR_USB -y --eject
```

#### Verify Database & Waveform Integrity
```bash
rbconvert verify /Volumes/YOUR_USB
```

#### Restore from Backup (.bak)
```bash
rbconvert restore /Volumes/YOUR_USB
```

#### List Detected Drives & Profiles
```bash
rbconvert drives
rbconvert profiles
```

---

## Running Tests

Run the test suite via `uv`:
```bash
uv run pytest -v
```

---

## License

MIT License.
