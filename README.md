# Rekordbox Compatibility Converter

[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A cross-platform CLI and desktop GUI for checking an exported Rekordbox USB and converting incompatible audio to CDJ/XDJ-compatible AIFF, WAV, or MP3.

The converter updates the DeviceSQL `PIONEER/rekordbox/export.pdb` database—including its audio-format code—and referenced `ANLZxxxx.DAT`/`.EXT`/`.2EX` analysis paths. It stages output under unique temporary names, refuses target collisions and unsafe paths, commits each database update durably, and only then removes the original audio when requested.

## Why it exists

Many established club players, including the CDJ-2000NXS, CDJ-900NXS, XDJ-1000 MK1, XDJ-700, XDJ-RX, and original CDJ-2000, cannot play FLAC, ALAC, or high-resolution PCM files. Unsupported tracks can fail with `E-8302: CANNOT PLAY TRACK` or `UNSUPPORTED FILE FORMAT`.

Manual conversion breaks the paths stored in Rekordbox's exported database and analysis files. This tool keeps those references synchronized for DeviceSQL exports.

## Capabilities

- Converts incompatible audio in parallel with FFmpeg.
- Produces 16/24-bit AIFF or WAV, or 320 kbps MP3.
- Distinguishes AAC from ALAC inside ambiguous `.m4a` containers with `ffprobe`.
- Updates `export.pdb` filenames, paths, sizes, sample rates, bit depths, and bitrates.
- Updates PPTH paths in referenced ANLZ `.DAT`, `.EXT`, and `.2EX` files.
- Verifies that audio is decodable and that its extension, DeviceSQL format code, sample rate, bit depth, and size agree with database metadata.
- Detects missing, malformed, or mismatched ANLZ paths.
- Rejects DeviceSQL databases whose header sequence is inconsistent with their pages.
- Removes AppleDouble (`._*`) and `.DS_Store` files when enabled.
- Can hash-verify original audio and all Rekordbox metadata in a user-selected local recovery archive before removing USB originals.
- Separately checks final USB growth and local archive capacity, avoiding the need to fit originals and every converted file on the USB together.
- Refuses collisions, paths outside the selected USB, missing sidecars, and unsupported OneLibrary exports before conversion starts.

## Compatibility profiles

| Profile | Target hardware | Rules |
| :--- | :--- | :--- |
| `standard` | Broad club coverage: CDJ-350/850/900/2000 families, XDJ-AERO, XDJ-RX/RX2, XDJ-700/1000 | Accepts MP3/AAC/WAV/AIFF through 48 kHz and converts FLAC/ALAC. Allows 16/24-bit PCM. |
| `maximum` | Deliberately conservative fallback | Keeps only MP3/WAV/AIFF and normalizes conversion output to 16-bit, 44.1 kHz. This is stricter than many older players' published limits. |
| `modern` | CDJ-2000NXS2, CDJ-TOUR1, CDJ-3000/3000X, XDJ-1000MK2, XDJ-AN/AZ, OPUS-QUAD, OMNIS-DUO | Adds both FLAC and ALAC while retaining a shared 48 kHz ceiling across the explicitly listed models. XDJ-XZ and XDJ-RX3 publish FLAC but not ALAC support, so they are intentionally excluded from this shared guarantee. |

The optional **Enforce 16-bit lossless audio** policy is independent of these profiles. It also schedules otherwise-compatible 24-bit WAV/AIFF/FLAC/ALAC tracks for conversion and dithers their output to 16-bit. MP3 and AAC are not reconverted merely because of this setting. Player specifications vary by exact model and firmware; the GUI's profile help explains the conservative grouping and users should verify a venue's precise manual.

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

The release also includes `SHA256SUMS.txt` for download verification. The applications bundle Python, project dependencies, and purpose-built FFmpeg and ffprobe executables; users do not need to install a separate runtime. The bundled FFmpeg is compiled from pinned source with networking, GPL components, nonfree components, and unrelated video codecs disabled. It supports only the local audio formats, cover artwork, hashing, resampling, and AIFF/WAV/MP3 output used by this application.

Every application is accompanied by a platform-labelled `FFmpeg-Sources.tar.gz` asset containing the exact FFmpeg and LAME source archives, pinned checksums, licences, and reproducible build script. The app contains its own MIT licence, FFmpeg and LAME licence texts, exact FFmpeg build configuration, third-party notices, and a corresponding-source pointer. The frozen conversion smoke test rejects broad GPL/nonfree builds or incomplete legal metadata before an artifact is uploaded.

The desktop build workflow can also be started manually from GitHub Actions. Manual runs create downloadable test artifacts for both macOS architectures and Windows without publishing a GitHub Release. Each frozen application runs real AIFF, WAV, and MP3 conversions with the system `PATH` disabled before its artifact is uploaded.

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
uv run rbconvert convert /Volumes/YOUR_USB --profile standard --enforce-16-bit --yes
uv run rbconvert convert /Volumes/YOUR_USB --format mp3 --threads 1 --yes
uv run rbconvert convert /Volumes/YOUR_USB --keep-originals --yes
uv run rbconvert convert /Volumes/YOUR_USB --original-backup-dir "$HOME/Music/Rekordbox Backups" --yes
uv run rbconvert convert /Volumes/YOUR_USB --yes --eject
```

Conversion defaults to 2 parallel workers. USB flash drives often become slower with high
parallelism because source reads and converted-file writes compete on the same device.

Verify actual audio, database metadata, profile compatibility, and ANLZ references:

```bash
uv run rbconvert verify /Volumes/YOUR_USB --profile standard
```

Restore a conversion from its local recovery session, including original audio, Device
Library, OneLibrary, and referenced ANLZ files:

```bash
uv run rbconvert restore-local-backup "/path/to/RekordboxBackup-session" --usb /Volumes/YOUR_USB
```

List detected drives and profiles:

```bash
uv run rbconvert drives
uv run rbconvert profiles
```

### Backup restoration

When `--original-backup-dir` is used, the converter creates a self-contained recovery session outside the USB. It copies and SHA-256 verifies every affected original plus Device Library, OneLibrary, and ANLZ metadata before removing any original from the USB. The manifest records converted-file hashes and refuses restoration after unrelated USB changes. Existing conversion targets require separate confirmation. Targets already referenced by Rekordbox are never overwritten: the converter creates a staged output and reuses the referenced file only when their decoded-audio SHA-256 hashes match. Unreferenced targets are archived and verified before replacement, and are included in rollback and full restoration.

Without a local archive, database and ANLZ `.bak` files are created on the USB by default. Legacy restoration is deliberately refused if the backup's referenced original audio files are missing or have changed, because restoring metadata alone would create broken track references.

Legacy `.bak` restoration does not restore OneLibrary. If OneLibrary has already been rebuilt from a converted Device Library, run **Convert from Device Library** in Rekordbox again after restoring so the two databases do not remain inconsistent. A local recovery session does include the pre-conversion OneLibrary file and restores it only while the USB still matches the recorded post-conversion hashes.

Restoration is therefore useful when originals were retained:

```bash
uv run rbconvert restore /Volumes/YOUR_USB
```

Backups are not substitutes for a separate copy of the USB. Keep an external backup before modifying a performance library.

## Desktop GUI

```bash
uv run rbconvert-gui
```

The GUI supports dark, light, and system appearance modes, drive detection, profile descriptions, a whole-library 16-bit lossless policy, conversion controls, a local recovery-folder selector, full local restore, a track table, and progress reporting. The **?** button beside the profile selector and the **Help** menu keep the profile table and OneLibrary workflow available at any time. **Help → About & Open Source Licences** exposes the application licence, third-party notices, FFmpeg/LAME terms, exact build information, and corresponding-source location in packaged releases. The visible **Show guidance popups** switch in the header, mirrored under **Settings**, suppresses repeat educational popups; errors, destructive-action confirmations, and recovery warnings remain visible.

## OneLibrary limitation

`exportLibrary.db` is the OneLibrary database, previously called Device Library Plus. It has different synchronization requirements from the traditional DeviceSQL `export.pdb`. This release detects it but does not modify it. By default, OneLibrary causes conversion to stop before audio or database files are changed; the explicit experimental bridge described below updates only Device Library and requires a subsequent Rekordbox rebuild.

Current Rekordbox versions normally maintain OneLibrary and Device Library side by side. This means simply re-exporting may create OneLibrary again. If the converter changed only `export.pdb`, OneLibrary would retain the old audio paths and equipment that reads OneLibrary could show missing tracks. Do not manually delete `exportLibrary.db` from a working USB: newer OneLibrary-only equipment does not reliably fall back to the traditional database. See AlphaTheta's [OneLibrary-compatible USB export guide](https://cdn.rekordbox.com/files/20260318114024/OneLibrary-Compatible-USB-Device-Export_en.pdf) for the current device split and export workflow.

### Experimental OneLibrary bridge

AlphaTheta documents a **Convert from Device Library** command that overwrites OneLibrary with the traditional Device Library's content. The converter can use that as an explicitly experimental bridge; it still does not parse or modify `exportLibrary.db` itself.

Test this only on a complete copy of a USB:

1. Scan the USB in the GUI and accept **Two-Step Rekordbox Update Required**, or add `--experimental-onelibrary-bridge` to the CLI command.
2. Select a local original-backup folder. The app verifies every affected original and all Rekordbox metadata there before removing those originals from the USB.
3. Click **Convert Tracks (Step 1 of 2)**. Conversion reads from the local archive, so only the net growth of the final converted library must fit on the USB.
4. Open the USB in the latest Rekordbox. Do not export or synchronize other content first.
5. Under **Devices**, right-click **OneLibrary** and choose **Convert from Device Library**. In Rekordbox 6, the menu is named **Device Library Plus**. Accept the overwrite warnings.
6. Inspect the USB's OneLibrary view and verify the converted tracks before using the USB on equipment.
7. Keep the local recovery session until Rekordbox and CDJ testing are complete.

```bash
uv run rbconvert convert /Volumes/YOUR_USB --experimental-onelibrary-bridge \
  --original-backup-dir "$HOME/Music/Rekordbox Backups" \
  --replace-existing-targets
```

The final option is needed only when files already occupy one or more planned conversion paths. Without it, the interactive CLI asks separately; `--yes` still requires this explicit option.

The Rekordbox command overwrites the existing OneLibrary. AlphaTheta warns that playlists or playback histories stored only in OneLibrary will be lost. Between conversion and that rebuild, OneLibrary still points at removed originals and the USB must not be used on OneLibrary equipment. The converter does not claim that moving originals off-device can make an oversized final library fit: lossless AIFF can be substantially larger than FLAC, and the final converted files plus metadata must still fit on the USB.

## Development and tests

Desktop release builds compile the pinned minimal FFmpeg and LAME sources with:

```bash
packaging/ffmpeg/build_minimal_ffmpeg.sh build-vendor/minimal-ffmpeg
```

The script verifies source checksums and emits binaries, licence files, and a
corresponding-source archive. Source-checkout development can continue using a
system FFmpeg installation.

```bash
uv sync --extra dev
uv run --extra dev pytest -v
```

The CI matrix covers Python 3.9 through 3.14 and Linux, macOS, and Windows.

Pushing a version tag that exactly matches the package version triggers the release build. For example, version `0.2.0` requires tag `v0.2.0`. The workflow tests the project first, builds all three desktop targets on their native operating systems, smoke-tests the packaged applications, and publishes one GitHub Release with generated notes and checksums.

## License

[MIT](LICENSE)
