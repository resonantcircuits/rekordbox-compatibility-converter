"""CLI behavior and process-status tests."""

from types import SimpleNamespace

from click.testing import CliRunner

from rekordbox_compatibility_converter.cli import main
from rekordbox_compatibility_converter.cli.main import cli


def test_scan_missing_database_returns_nonzero(tmp_path):
    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code != 0
    assert "export.pdb" in result.output


def test_scan_malformed_database_returns_friendly_error(tmp_path):
    database = tmp_path / "PIONEER" / "rekordbox" / "export.pdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"corrupt")

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code != 0
    assert "Failed to parse Rekordbox Device Library" in result.output
    assert result.exception is not None


def test_convert_help_exposes_whole_library_16_bit_policy():
    result = CliRunner().invoke(cli, ["convert", "--help"])

    assert result.exit_code == 0
    assert "--enforce-16-bit" in result.output
    assert "otherwise-compatible WAV/AIFF" in result.output


def test_folder_help_exposes_safe_standalone_workflow():
    result = CliRunner().invoke(cli, ["folder", "--help"])

    assert result.exit_code == 0
    assert "--output" in result.output
    assert "--normalize-all" in result.output
    assert "--copy-compatible" in result.output
    assert "--converted-only" in result.output


def test_folder_command_creates_audio_only_collection(tmp_path, monkeypatch):
    source = tmp_path / "Source"
    destination = tmp_path / "Destination"
    source.mkdir()
    task = SimpleNamespace(action="convert", audio=SimpleNamespace(filename="track.flac"))
    summary = SimpleNamespace(
        source_root=source.resolve(),
        destination_root=destination.resolve(),
        total_files=1,
        conversion_files=1,
        copy_files=0,
        issues=[],
        warnings=[],
        tasks=[task],
    )
    calls = []

    class FakeFolderEngine:
        audio_converter = SimpleNamespace(check_tools=lambda: (True, "available"))

        def scan(self, *args, **kwargs):
            calls.append((args, kwargs))
            return summary

        def execute(self, received_summary, threads, progress_callback):
            assert received_summary is summary
            assert threads == 3
            progress_callback(task, 1, 1)
            return {
                "success": True,
                "completed": 1,
                "converted": 1,
                "copied": 0,
                "failed": 0,
                "errors": [],
                "destination": str(destination),
            }

    monkeypatch.setattr(main, "FolderConversionEngine", FakeFolderEngine)

    result = CliRunner().invoke(
        cli,
        [
            "folder",
            str(source),
            "--output",
            str(destination),
            "--threads",
            "3",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Compatible collection created" in result.output
    assert "audio files only" in result.output
    assert calls[0][1]["copy_compatible"] is True


def test_scan_device_library_plus_returns_nonzero(tmp_path):
    database = tmp_path / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"unsupported")

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code != 0
    assert "OneLibrary" in result.output
    assert "No files were changed" in result.output
    assert "will not reliably fall back" in result.output


def test_scan_reports_compatible_tracks_with_stale_waveform_paths(
    tmp_path, monkeypatch
):
    summary = SimpleNamespace(
        has_export_pdb=True,
        has_dlp=False,
        onelibrary_bridge_mode=False,
        unsupported_reason="",
        format_counts={"mp3": 1},
        total_tracks=1,
        compatible_tracks=1,
        incompatible_tracks=0,
        analysis_repairs=[
            SimpleNamespace(
                track=SimpleNamespace(id=42, title="Test Track", filename="test.mp3"),
                old_audio_path="/Contents/test.flac",
                new_audio_path="/Contents/test.mp3",
            )
        ],
        bitrate_repairs=[],
    )

    class FakeEngine:
        def scan(self, **_kwargs):
            return summary

    monkeypatch.setattr(main, "ConversionEngine", FakeEngine)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code == 0
    assert "Waveform Paths to Repair" in result.output
    assert "Test Track" in result.output
    assert "stored waveform paths need repair" in result.output
    assert "All tracks are compatible with the selected profile" not in result.output


def test_convert_cleans_dotfiles_when_no_audio_conversion_is_needed(
    tmp_path, monkeypatch
):
    cleaned = []
    summary = SimpleNamespace(
        has_export_pdb=True,
        has_dlp=False,
        onelibrary_bridge_mode=False,
        incompatible_tracks=0,
        analysis_repairs=[],
        bitrate_repairs=[],
        tasks=[],
    )

    class FakeEngine:
        def scan(self, **_kwargs):
            return summary

        def clean_dotfiles(self, usb_root):
            cleaned.append(usb_root)
            return 2

    monkeypatch.setattr(main, "ConversionEngine", FakeEngine)
    monkeypatch.setattr(
        main,
        "AudioConverter",
        lambda: SimpleNamespace(check_tools=lambda: (True, "available")),
    )

    result = CliRunner().invoke(cli, ["convert", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert cleaned == [tmp_path.resolve()]
    assert "Cleaned 2 macOS ghost file(s)" in result.output


def test_cleanup_originals_cli_runs_verified_plan(tmp_path, monkeypatch):
    plan = SimpleNamespace(
        errors=[],
        candidates=[SimpleNamespace()],
        total_bytes=2 * 1024 ** 3,
        warnings=["Confirm the converted tracks in Rekordbox."],
    )

    class FakeEngine:
        def plan_retained_original_cleanup(self, usb_root):
            assert usb_root == tmp_path.resolve()
            return plan

        def cleanup_retained_originals(self, cleanup_plan):
            assert cleanup_plan is plan
            return {
                "success": True,
                "removed": 1,
                "failed": 0,
                "freed_bytes": plan.total_bytes,
            }

    monkeypatch.setattr(main, "ConversionEngine", FakeEngine)

    result = CliRunner().invoke(
        cli,
        ["cleanup-originals", str(tmp_path), "--yes"],
    )

    assert result.exit_code == 0
    assert "Removed 1 verified originals" in result.output
    assert "2.00 GiB" in result.output


def test_restore_local_backup_cli_uses_selected_usb(tmp_path, monkeypatch):
    session = tmp_path / "RekordboxBackup-session"
    usb = tmp_path / "USB"
    session.mkdir()
    usb.mkdir()
    calls = []

    class FakeEngine:
        def restore_local_backup(self, session_dir, usb_root):
            calls.append((session_dir, usb_root))
            return True, "Local backup restored"

    monkeypatch.setattr(main, "ConversionEngine", FakeEngine)
    result = CliRunner().invoke(
        cli,
        [
            "restore-local-backup",
            str(session),
            "--usb",
            str(usb),
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert calls == [(session.resolve(), usb.resolve())]
    assert "Local backup restored" in result.output
