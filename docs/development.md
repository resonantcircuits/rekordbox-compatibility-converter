# Development, packaging, and releases

## Development setup

Use `uv` for the environment and all Python commands:

```bash
uv sync --extra dev
uv run --extra dev pytest -v
```

Source-checkout development uses FFmpeg and ffprobe from `PATH`. The project supports Python 3.9 and newer.

## Minimal FFmpeg build

Desktop packages use pinned, minimal FFmpeg and LAME builds:

```bash
packaging/ffmpeg/build_minimal_ffmpeg.sh build-vendor/minimal-ffmpeg
```

The script verifies source checksums and produces the runtime binaries, licences, and corresponding-source archive. Optimized x86 and x86-64 builds require NASM.

GitHub Actions caches only the finished binaries, legal directory, and corresponding-source archive. The cache key includes the target platform and a hash of the build script, pinned versions, and source README. A changed input creates a new cache; partial or fallback matches are not used. Release-specific source names and URLs are generated after restoration rather than stored in the cache.

## CI and test builds

Pushes and pull requests cover Python 3.9 through 3.14 on Linux, with endpoint coverage on macOS and Windows. A manual **CI & Release Build** run also builds and smoke-tests:

- macOS on Apple Silicon
- macOS on Intel
- Windows x86-64

Manual runs upload test artifacts for 14 days but do not publish a release. Each frozen app performs real AIFF, WAV, and MP3 conversions with the system `PATH` removed before upload.

## Publishing a release

The project version and tag must match exactly. For version `0.2.0`, use tag `v0.2.0`:

```bash
git tag -a v0.2.0 -m "Release v0.2.0"
git push origin v0.2.0
```

The tag workflow runs tests, builds all three desktop targets, creates `SHA256SUMS.txt`, and publishes a GitHub Release with generated notes. The first build for a new FFmpeg cache key compiles from source; later matching runs restore the compact cache.

The release workflow is the only automated publisher. Do not create the GitHub Release manually before pushing the tag.
