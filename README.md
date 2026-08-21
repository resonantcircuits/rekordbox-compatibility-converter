# Rekordbox Compatibility Converter

[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A cross-platform CLI and desktop GUI for checking an exported Rekordbox USB and converting incompatible audio to CDJ/XDJ-compatible AIFF, WAV, or MP3.

The converter updates the DeviceSQL `PIONEER/rekordbox/export.pdb` database—including its audio-format code—and referenced `ANLZxxxx.DAT`/`.EXT` analysis paths. It stages output under unique temporary names, refuses target collisions and unsafe paths, commits each database update durably, and only then removes the original audio when requested.

## Why it exists

Many established club players, including the CDJ-2000NXS, CDJ-900NXS, XDJ-1000 MK1, XDJ-700, XDJ-RX, and original CDJ-2000, cannot play FLAC, ALAC, or high-resolution PCM files. Unsupported tracks can fail with `E-8302: CANNOT PLAY TRACK` or `UNSUPPORTED FILE FORMAT`.

Manual conversion breaks the paths stored in Rekordbox's exported database and analysis files. This tool keeps those references synchronized for DeviceSQL exports.

## Capabilities

- Converts incompatible audio in parallel with FFmpeg.
- Produces 16/24-bit AIFF or WAV, or 320 kbps MP3.
- Distinguishes AAC from ALAC inside ambiguous `.m4a` containers with `ffprobe`.
- Updates `export.pdb` filenames, paths, sizes, sample rates, bit depths, and bitrates.
- Updates PPTH paths in referenced ANLZ `.DAT` and `.EXT` files.
- Verifies that audio is decodable and that its extension, DeviceSQL format code, sample rate, bit depth, and size agree with database metadata.
- Detects missing, malformed, or mismatched ANLZ paths.
- Rejects DeviceSQL databases whose header sequence is inconsistent with their pages.
- Removes AppleDouble (`._*`) and `.DS_Store` files when enabled.
- Checks fresh free-space availability for worst-case parallel staging and backups.
- Safely identifies and removes originals retained until a OneLibrary rebuild is verified.
- Refuses collisions, paths outside the selected USB, missing sidecars, and unsupported OneLibrary exports before conversion starts.

## Compatibility profiles

| Profile | Target hardware | Rules |
| :--- | :--- | :--- |
| `standard` | CDJ-2000NXS, CDJ-900NXS, XDJ-1000/700/RX/RX2 | Converts FLAC/ALAC and PCM outside 44.1/48 kHz. Allows supported MP3/AAC rates. |
| `maximum` | CDJ-2000 original, CDJ-850, CDJ-350, XDJ-AERO | Enforces 16-bit, 44.1 kHz AIFF/WAV/MP3 output. |
| `modern` | CDJ-3000, CDJ-2000NXS2, XDJ-XZ, XDJ-RX3, OPUS-QUAD | Accepts FLAC, ALAC, AIFF, and WAV through 24-bit/96 kHz at supported discrete rates. |

## Requirements and installation

- Python 3.9 or newer
- FFmpeg and ffprobe available on `PATH`
- `uv`

Install FFmpeg with `brew install ffmpeg` on macOS, `choco install ffmpeg` on Windows, or your Linux distribution's package manager.

From an existing checkout:

```bash
uv sync --extra dev
```

### Desktop release downloads

Version tags publish these unsigned desktop builds to the corresponding GitHub Release:

- `macOS-arm64` for Apple Silicon Macs
- `macOS-x86_64` for Intel Macs
- `Windows-x86_64` for 64-bit Windows

The release also includes `SHA256SUMS.txt` for download verification. The applications bundle Python and project dependencies, but FFmpeg and ffprobe must still be installed separately and available on `PATH`.

The current builds are not code-signed or notarized. macOS Gatekeeper and Windows SmartScreen may therefore require the user to explicitly approve the first launch. Production signing requires Apple Developer and Windows code-signing credentials configured as repository secrets.

## CLI usage

Scan an explicitly selected export:

```bash
uv run rbconvert scan /Volumes/YOUR_USB
```

If exactly one Rekordbox USB is connected, the path may be omitted:

```bash
uv run rbconvert scan
```

Convert after an interactive confirmation:

```bash
uv run rbconvert convert /Volumes/YOUR_USB
```

Examples:

```bash
uv run rbconvert convert /Volumes/YOUR_USB --profile maximum --yes
uv run rbconvert convert /Volumes/YOUR_USB --format mp3 --threads 1 --yes
uv run rbconvert convert /Volumes/YOUR_USB --keep-originals --yes
uv run rbconvert convert /Volumes/YOUR_USB --yes --eject
```

Conversion defaults to 2 parallel workers. USB flash drives often become slower with high
parallelism because source reads and converted-file writes compete on the same device.

Verify actual audio, database metadata, profile compatibility, and ANLZ references:

```bash
uv run rbconvert verify /Volumes/YOUR_USB --profile standard
```

After completing and verifying Rekordbox's OneLibrary rebuild, reclaim the space used by
retained originals:

```bash
uv run rbconvert cleanup-originals /Volumes/YOUR_USB
```

List detected drives and profiles:

```bash
uv run rbconvert drives
uv run rbconvert profiles
```

### Backup restoration

Database and ANLZ backups are created by default. Restoration is deliberately refused if the backup's referenced original audio files are missing or have changed, because restoring metadata alone would create broken track references.

Restoration does not restore OneLibrary. If OneLibrary has already been rebuilt from a converted Device Library, run **Convert from Device Library** in Rekordbox again after restoring so the two databases do not remain inconsistent.

Restoration is therefore useful when originals were retained:

```bash
uv run rbconvert restore /Volumes/YOUR_USB
```

Backups are not substitutes for a separate copy of the USB. Keep an external backup before modifying a performance library.

## Desktop GUI

```bash
uv run rbconvert-gui
```

The GUI supports dark, light, and system appearance modes, drive detection, profile descriptions, conversion controls, a track table, and progress reporting.

## OneLibrary limitation

`exportLibrary.db` is the OneLibrary database, previously called Device Library Plus. It has different synchronization requirements from the traditional DeviceSQL `export.pdb`. This release detects it but does not modify it. By default, OneLibrary causes conversion to stop before audio or database files are changed; the explicit experimental bridge described below updates only Device Library and requires a subsequent Rekordbox rebuild.

Current Rekordbox versions normally maintain OneLibrary and Device Library side by side. This means simply re-exporting may create OneLibrary again. If the converter changed only `export.pdb`, OneLibrary would retain the old audio paths and equipment that reads OneLibrary could show missing tracks. Do not manually delete `exportLibrary.db` from a working USB: newer OneLibrary-only equipment does not reliably fall back to the traditional database. See AlphaTheta's [OneLibrary-compatible USB export guide](https://cdn.rekordbox.com/files/20260318114024/OneLibrary-Compatible-USB-Device-Export_en.pdf) for the current device split and export workflow.

### Experimental OneLibrary bridge

AlphaTheta documents a **Convert from Device Library** command that overwrites OneLibrary with the traditional Device Library's content. The converter can use that as an explicitly experimental bridge; it still does not parse or modify `exportLibrary.db` itself.

Test this only on a complete copy of a USB:

1. Scan the USB in the GUI and accept **Two-Step Rekordbox Update Required**, or add `--experimental-onelibrary-bridge` to the CLI command.
2. Click **Convert Tracks (Step 1 of 2)**. Bridge mode requires database backups and always retains every original audio file.
3. Open the USB in the latest Rekordbox. Do not export or synchronize other content first.
4. Under **Devices**, right-click **OneLibrary** and choose **Convert from Device Library**. In Rekordbox 6, the menu is named **Device Library Plus**. Accept the overwrite warnings.
5. Inspect the USB's OneLibrary view and verify the converted tracks before using the USB on equipment.
6. Return to the converter and click **Remove Retained Originals** to reclaim USB space. The cleanup verifies the pre-conversion database backup, every replacement file, current Device Library metadata, and ANLZ paths before asking for permanent-deletion confirmation.

```bash
uv run rbconvert convert /Volumes/YOUR_USB --experimental-onelibrary-bridge
```

The Rekordbox command overwrites the existing OneLibrary. AlphaTheta warns that playlists or playback histories stored only in OneLibrary will be lost. The originals are deliberately left on the USB so the existing OneLibrary links remain valid until the rebuild succeeds. Cleanup remains disabled until OneLibrary appears newer than the converted Device Library and still requires the user to confirm that the converted tracks were inspected in Rekordbox; this app cannot parse OneLibrary's opaque contents directly.

## Development and tests

```bash
uv sync --extra dev
uv run --extra dev pytest -v
```

The CI matrix covers Python 3.9 through 3.14 and Linux, macOS, and Windows.

Pushing a version tag that exactly matches the package version triggers the release build. For example, version `0.2.0` requires tag `v0.2.0`. The workflow tests the project first, builds all three desktop targets on their native operating systems, smoke-tests the packaged applications, and publishes one GitHub Release with generated notes and checksums.

## License

[MIT](LICENSE)
