# Installation

## Desktop releases

Download the current release from the [GitHub Releases page](https://github.com/resonantcircuits/rekordbox-compatibility-converter/releases).

Each release provides:

- `macOS-arm64` for Apple Silicon Macs
- `macOS-x86_64` for Intel Macs
- `Windows-x86_64` for 64-bit Windows
- `SHA256SUMS.txt` for download verification

The desktop packages contain Python, the application dependencies, and minimal FFmpeg and ffprobe builds. Users do not need to install those components separately.

The packages are not code-signed or notarized. On macOS, use Finder's **Open** command from the context menu if Gatekeeper blocks the first launch. On Windows, SmartScreen may require **More info → Run anyway**. Only download releases from this repository.

## Bundled FFmpeg

Release builds compile FFmpeg and LAME from pinned source. Networking, GPL components, nonfree components, and unrelated codecs are disabled. The resulting tools support the local audio formats, artwork, hashing, resampling, and AIFF/WAV/MP3 output required by the converter.

Each release has one shared `FFmpeg-Sources.tar.gz` asset containing the exact upstream archives, checksums, licences, and cross-platform build script for all bundled FFmpeg binaries. The packaged app also contains:

- The application MIT licence
- FFmpeg and LAME licence texts
- The exact FFmpeg build configuration
- Python, Tcl/Tk, and Python-package licences
- Third-party notices and a corresponding-source pointer

The frozen-app smoke test rejects GPL/nonfree FFmpeg builds, missing legal metadata, and binaries that cannot complete real AIFF, WAV, and MP3 conversions.

## Source installation

Source installs require:

- Python 3.9 or newer
- [`uv`](https://docs.astral.sh/uv/)
- FFmpeg and ffprobe on `PATH`

Install FFmpeg with `brew install ffmpeg` on macOS, `choco install ffmpeg` on Windows, or the package manager for your Linux distribution.

From a checkout:

```bash
uv sync --extra dev
uv run rbconvert-gui
```

For CLI use, see [Using the application](usage.md#command-line-interface).
