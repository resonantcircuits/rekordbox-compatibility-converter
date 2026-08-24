# Corresponding source for bundled FFmpeg and LAME

This archive accompanies the FFmpeg and ffprobe executables distributed with
Rekordbox Format Checker. It contains the exact, unmodified upstream source
archives and build script used to create them.

Run `build_minimal_ffmpeg.sh OUTPUT_DIRECTORY` in a POSIX shell with a C
toolchain, `make`, `pkg-config`, `curl`, `tar`, and zlib development files.
Optimized x86 and x86-64 builds also require NASM. On Windows, use the MINGW64
environment supplied by MSYS2. See the project's GitHub Actions workflow for
the exact CI environment.

FFmpeg is built without `--enable-gpl` and without `--enable-nonfree`. The
resulting FFmpeg build is licensed under LGPL 2.1 or later. LAME is licensed
under LGPL 2.0 or later. See the source archives for complete licence terms.

Project build scripts and release history:
https://github.com/resonantcircuits/rekordbox-compatibility-converter
