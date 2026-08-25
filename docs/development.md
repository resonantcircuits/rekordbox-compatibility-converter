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

The committed project version, lockfile, and tag must match exactly. Before creating
the tag:

```bash
uv lock
RELEASE_TAG=v0.4.0 uv run --frozen --extra dev python -c "import os, tomllib; version = tomllib.load(open('pyproject.toml', 'rb'))['project']['version']; expected = 'v' + version; actual = os.environ['RELEASE_TAG']; assert actual == expected, 'tag %r does not match package version %s' % (actual, expected)"
git add pyproject.toml uv.lock
git commit -m "Prepare v0.4.0 release"
git tag -a v0.4.0 -m "Release v0.4.0"
git push origin main v0.4.0
```

Replace `0.4.0` with the intended release version in all commands. Create the tag
only after the version commit exists; verify the tag points to that commit before
pushing it.

The tag workflow runs tests, builds all three desktop targets, publishes one shared corresponding-source archive, creates `SHA256SUMS.txt`, and publishes a GitHub Release with generated notes. The first build for a new FFmpeg cache key compiles from source; later matching runs restore the compact cache.

The release workflow is the only automated publisher. Do not create the GitHub Release manually before pushing the tag.
