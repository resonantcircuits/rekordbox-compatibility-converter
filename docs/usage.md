# Using the application

## Desktop application

Packaged releases open directly as a desktop application. From a source checkout:

```bash
uv run rbconvert-gui
```

The main workflow is:

1. Select a detected Rekordbox USB.
2. Choose a compatibility profile and optional 16-bit policy.
3. Scan and review the tracks that require conversion.
4. Select AIFF, WAV, or MP3 output.
5. Choose a local recovery folder unless originals will remain on the USB.
6. Start conversion and wait for verification to finish.
7. Test the result in Rekordbox and on the target player.

The profile information button lists the covered hardware. The **Help** menu keeps compatibility, OneLibrary workflow, application information, and open-source licences available. **Show guidance popups** controls repeat educational messages; errors, destructive-action confirmations, and recovery warnings remain enabled.

Conversion defaults to two workers. Increasing the count can make USB flash drives slower because reads and writes compete on the same device.

## Command-line interface

Scan an explicitly selected USB:

```bash
uv run rbconvert scan /Volumes/YOUR_USB
```

If exactly one Rekordbox USB is connected, the path may be omitted:

```bash
uv run rbconvert scan
```

Convert after interactive confirmation:

```bash
uv run rbconvert convert /Volumes/YOUR_USB
```

Common variants:

```bash
uv run rbconvert convert /Volumes/YOUR_USB --profile maximum --yes
uv run rbconvert convert /Volumes/YOUR_USB --profile standard --enforce-16-bit --yes
uv run rbconvert convert /Volumes/YOUR_USB --format mp3 --threads 1 --yes
uv run rbconvert convert /Volumes/YOUR_USB --keep-originals --yes
uv run rbconvert convert /Volumes/YOUR_USB --original-backup-dir "/path/to/Rekordbox Backups" --yes
uv run rbconvert convert /Volumes/YOUR_USB --yes --eject
```

Verify audio, Device Library metadata, profile compatibility, and analysis references:

```bash
uv run rbconvert verify /Volumes/YOUR_USB --profile standard
```

Restore a local recovery session:

```bash
uv run rbconvert restore-local-backup "/path/to/RekordboxBackup-session" --usb /Volumes/YOUR_USB
```

Restore legacy on-USB backups when their original audio is still present:

```bash
uv run rbconvert restore /Volumes/YOUR_USB
```

List detected drives and profiles:

```bash
uv run rbconvert drives
uv run rbconvert profiles
```

Run `uv run rbconvert --help` or `uv run rbconvert COMMAND --help` for the complete option list.

See [Recovery archives and OneLibrary](recovery-and-onelibrary.md) before removing originals from a USB that contains OneLibrary.
