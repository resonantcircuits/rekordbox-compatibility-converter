# Recovery archives and OneLibrary

Keep an independent backup of a performance USB before conversion. The recovery features described here protect a conversion session; they are not a replacement for a separate copy of the library.

## Local recovery archives

When a local recovery folder is selected, the converter creates a self-contained session outside the USB. Before removing anything from the USB, it copies and SHA-256 verifies:

- Every affected original track
- The traditional Device Library
- OneLibrary, when present
- Every referenced `.DAT`, `.EXT`, and `.2EX` analysis file
- Pre-existing files that must be replaced at planned target paths

The manifest records the converted-file and metadata hashes. Restoration stops if unrelated changes make the USB inconsistent with the recorded post-conversion state.

Files already referenced by Rekordbox are never overwritten blindly. When the original is available, the converter stages a new output and reuses the referenced target only when their decoded-audio SHA-256 hashes match. If Rekordbox reintroduces a database row for an original it did not restore, the converter can relink that row to an existing converted file only when the title, duration, filename, DeviceSQL path, analysis path, file size, codec, sample rate, and bit depth all agree. The existing audio is not modified. Unreferenced targets require separate replacement confirmation and are added to rollback and full restoration.

The scan also detects compatible tracks whose Device Library path already points to converted audio while all existing `.DAT`, `.EXT`, and `.2EX` files still contain the missing original's extension-only path. These waveform paths can be repaired without reconverting or modifying the audio. Different directories, filename stems, mixed sidecar paths, existing original files, or malformed sidecars are refused.

Storing originals on the computer removes the old requirement to fit every original and every converted file on the USB at the same time. The final converted library still has to fit; AIFF can be substantially larger than FLAC.

## Legacy on-USB backups

Without a local recovery archive, Device Library and analysis `.bak` files are created on the USB by default. Legacy restoration is refused when referenced original audio is missing or changed, because restoring metadata alone would leave broken track paths.

Legacy `.bak` restoration does not restore OneLibrary. If OneLibrary was rebuilt after conversion, use **Convert from Device Library** in Rekordbox again after a legacy restore. A local recovery session includes the pre-conversion OneLibrary file and restores it only while the USB still matches the recorded hashes.

## OneLibrary limitation

`exportLibrary.db` is the OneLibrary database, previously called Device Library Plus. It is separate from the traditional DeviceSQL `export.pdb` and has different synchronization requirements. This release detects OneLibrary but does not parse or modify it.

By default, conversion stops before changing audio or metadata when OneLibrary is present. Do not delete `exportLibrary.db` from a working USB: newer OneLibrary-only equipment does not reliably fall back to the traditional Device Library.

AlphaTheta's [OneLibrary-compatible USB export guide](https://cdn.rekordbox.com/files/20260318114024/OneLibrary-Compatible-USB-Device-Export_en.pdf) describes the current device split and Rekordbox workflow.

## Experimental two-step bridge

Rekordbox provides **Convert from Device Library**, which overwrites OneLibrary with the traditional Device Library's content. The converter can use that command as an explicit two-step bridge without modifying OneLibrary itself.

Test this only on a complete copy of a USB:

1. Scan the USB and accept **Two-Step Rekordbox Update Required**, or use `--experimental-onelibrary-bridge` in the CLI.
2. Select a local recovery folder. The app verifies affected originals and Rekordbox metadata there before removing originals from the USB.
3. Run **Convert Tracks (Step 1 of 2)**.
4. Open the USB in the latest Rekordbox without exporting or synchronizing other content first.
5. Under **Devices**, right-click **OneLibrary** and choose **Convert from Device Library**. Rekordbox 6 calls it **Device Library Plus**.
6. Accept the overwrite warnings, inspect OneLibrary, and verify converted tracks.
7. Keep the recovery session until Rekordbox and player testing are complete.

CLI example:

```bash
uv run rbconvert convert /Volumes/YOUR_USB --experimental-onelibrary-bridge \
  --original-backup-dir "/path/to/Rekordbox Backups" \
  --replace-existing-targets
```

`--replace-existing-targets` is required only when files already occupy planned target paths. `--yes` does not grant this separate replacement permission.

The Rekordbox command overwrites OneLibrary. Playlists or histories stored only there can be lost. Between conversion and the Rekordbox rebuild, OneLibrary still points to removed originals and the USB must not be used on OneLibrary equipment.
