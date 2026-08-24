# Compatibility profiles

Older club players often reject FLAC, ALAC, or PCM audio above 48 kHz with `E-8302: CANNOT PLAY TRACK` or `UNSUPPORTED FILE FORMAT`. Converting files outside Rekordbox also changes filenames and paths stored in the exported Device Library and analysis files. The converter changes the audio and those references together.

## Profiles

| Profile | Explicitly covered players | Rules |
| :--- | :--- | :--- |
| **Standard Club** (`standard`) | Baseline model list below | Accepts MP3, AAC, WAV, and AIFF through 48 kHz. Converts FLAC and ALAC. Allows 16-bit and 24-bit PCM. |
| **Conservative 16-bit** (`maximum`) | Baseline model list below | Keeps only MP3, WAV, and AIFF. Conversion output is normalized to 16-bit, 44.1 kHz. This is intentionally stricter than many published player limits. |
| **Modern Lossless** (`modern`) | Modern lossless model list below | Adds FLAC and ALAC while retaining a shared 48 kHz ceiling across this group. |

### Baseline model list

Standard Club and Conservative 16-bit explicitly cover:

- CDJ-350, CDJ-850, CDJ-900, CDJ-900NXS
- CDJ-2000, CDJ-2000NXS, CDJ-2000NXS2, CDJ-TOUR1
- CDJ-3000, CDJ-3000X
- XDJ-AERO, XDJ-R1, XDJ-RR
- XDJ-RX, XDJ-RX2, XDJ-RX3, XDJ-RZ, XDJ-XZ
- XDJ-700, XDJ-1000, XDJ-1000MK2
- XDJ-AN, XDJ-AZ
- OPUS-QUAD, OMNIS-DUO

### Modern lossless model list

Modern Lossless explicitly covers:

- CDJ-2000NXS2, CDJ-TOUR1, CDJ-3000, CDJ-3000X
- XDJ-1000MK2, XDJ-AN, XDJ-AZ
- OPUS-QUAD, OMNIS-DUO

XDJ-XZ and XDJ-RX3 publish FLAC support but not ALAC support, so they are not included in the shared Modern Players guarantee. Player limits can vary with firmware. Check the exact manual when preparing media for equipment outside the table.

## Enforce 16-bit lossless audio

The **Enforce 16-bit lossless audio** setting is independent of the selected profile. It schedules otherwise-compatible 24-bit WAV, AIFF, FLAC, and ALAC tracks for conversion and applies TPDF dithering to 16-bit output. MP3 and AAC are not reconverted solely because this setting is enabled.

Use this when the destination hardware accepts the profile's formats and sample rates but requires 16-bit PCM.

## Conversion and validation

The converter:

- Uses ffprobe to distinguish AAC from ALAC in `.m4a` containers.
- Produces 16-bit or 24-bit AIFF/WAV, or 320 kbps MP3.
- Updates Device Library filenames, paths, format codes, sizes, sample rates, bit depths, and bitrates.
- Updates `PPTH` paths in referenced `.DAT`, `.EXT`, and `.2EX` analysis files.
- Writes converted output under a unique temporary name, verifies it, and commits the audio and metadata changes before removing an original.
- Verifies decoded audio, extensions, format codes, metadata, and analysis references after conversion.
- Rejects malformed databases, missing sidecars, target collisions, and paths outside the selected USB before conversion.

AIFF is the default because uncompressed PCM preserves sample timing and avoids beatgrid drift caused by lossy transcoding.
