"""End-to-end integration tests for ConversionEngine with multi-threading, dotfile cleanup, and restore."""

import subprocess
import hashlib
import struct
from pathlib import Path
import pytest
from rekordbox_compatibility_converter.core.engine import ConversionEngine
from rekordbox_compatibility_converter.core.anlz_manager import ANLZManager
from rekordbox_compatibility_converter.core.models import CompatibilityProfileType, TargetFormat
from rekordbox_compatibility_converter.core.pdb_manager import PDBManager
from rekordbox_compatibility_converter.core.profiles import get_profile
from rekordbox_compatibility_converter.core.validator import ExportValidator
from tests.test_pdb import create_minimal_pdb
from tests.test_anlz import create_minimal_anlz


@pytest.fixture
def mock_usb(tmp_path: Path) -> Path:
    """Creates a mock Rekordbox USB drive structure with export.pdb, ANLZ, and FLAC audio."""
    usb_dir = tmp_path / "MOCK_USB"
    pioneer_dir = usb_dir / "PIONEER" / "rekordbox"
    anlz_dir = usb_dir / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    contents_dir = usb_dir / "Contents"

    pioneer_dir.mkdir(parents=True, exist_ok=True)
    anlz_dir.mkdir(parents=True, exist_ok=True)
    contents_dir.mkdir(parents=True, exist_ok=True)

    # Create FLAC file at /Contents/song.flac
    flac_path = contents_dir / "song.flac"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "sine=frequency=440:duration=0.5",
        "-ar", "44100",
        str(flac_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    # Create mock macOS ghost files
    (contents_dir / "._song.flac").write_bytes(b"ghost_resource_fork")
    (contents_dir / ".DS_Store").write_bytes(b"ds_store")

    # Create export.pdb
    create_minimal_pdb(
        pioneer_dir,
        file_size=flac_path.stat().st_size,
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
    )

    # Create ANLZ
    create_minimal_anlz(anlz_dir, "/Contents/song.flac")

    return usb_dir


def test_engine_scan_and_execute_parallel(mock_usb: Path):
    engine = ConversionEngine()
    profile = get_profile(CompatibilityProfileType.STANDARD)

    # Scan
    summary = engine.scan(
        usb_root=mock_usb,
        profile=profile,
        forced_target_format=TargetFormat.AIFF,
    )

    assert summary.has_export_pdb is True
    assert summary.total_tracks == 1
    assert summary.incompatible_tracks == 1
    assert len(summary.tasks) == 1

    task = summary.tasks[0]
    assert task.target_filename == "song.aiff"
    assert task.target_usb_path == "/Contents/song.aiff"

    # Execute conversion with 2 parallel workers and dotfile cleanup
    result = engine.execute(summary=summary, delete_original=True, backup=True, threads=2, clean_dotfiles=True)
    assert result.get("success") is True
    assert result.get("completed") == 1
    assert result.get("anlz_updated") == 1
    assert result.get("cleaned_dotfiles") >= 1

    # Verify converted AIFF exists
    aiff_path = mock_usb / "Contents" / "song.aiff"
    assert aiff_path.exists()

    # Verify original FLAC was deleted
    flac_path = mock_usb / "Contents" / "song.flac"
    assert not flac_path.exists()

    # Verify ghost files were cleaned
    assert not (mock_usb / "Contents" / "._song.flac").exists()
    assert not (mock_usb / "Contents" / ".DS_Store").exists()

    assert ANLZManager.read_path(
        mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001" / "ANLZ0000.DAT"
    ) == "/Contents/song.aiff"

    validation = ExportValidator().validate(mock_usb, profile)
    assert validation.failed_tracks == 0
    assert validation.issues == []

    # A database-only restore is refused after the original audio was deleted.
    success, msg = engine.restore_backup(mock_usb)
    assert success is False
    assert "missing or changed" in msg
    assert (mock_usb / "PIONEER" / "rekordbox" / "export.pdb").exists()


def test_engine_refuses_existing_target_without_overwriting(mock_usb: Path):
    target = mock_usb / "Contents" / "song.aiff"
    target.write_bytes(b"existing user file")
    summary = ConversionEngine().scan(mock_usb)

    result = ConversionEngine().execute(summary, clean_dotfiles=False)

    assert result["success"] is False
    assert target.read_bytes() == b"existing user file"
    assert (mock_usb / "Contents" / "song.flac").exists()


def test_engine_rolls_back_database_when_anlz_update_fails(mock_usb: Path, monkeypatch):
    monkeypatch.setattr(ANLZManager, "update_path", staticmethod(lambda *_args, **_kwargs: False))
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)

    result = engine.execute(summary, backup=False, clean_dotfiles=False, threads=1)

    assert result["success"] is False
    assert (mock_usb / "Contents" / "song.flac").exists()
    assert not (mock_usb / "Contents" / "song.aiff").exists()
    assert PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb").tracks[0].file_path.endswith(".flac")


def test_progress_callback_failure_does_not_interrupt_commit(mock_usb: Path):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)

    def broken_callback(*_args):
        raise RuntimeError("UI closed")

    result = engine.execute(
        summary,
        backup=False,
        clean_dotfiles=False,
        threads=1,
        progress_callback=broken_callback,
    )

    assert result["success"] is True
    assert any("Progress callback failed" in warning for warning in result["warnings"])
    assert PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb").tracks[0].file_path.endswith(".aiff")


def test_engine_refuses_device_library_plus(mock_usb: Path):
    dlp = mock_usb / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    dlp.parent.mkdir(parents=True)
    dlp.write_bytes(b"encrypted database")
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)

    result = engine.execute(summary, clean_dotfiles=False)

    assert result["success"] is False
    assert "old audio paths" in result["error"]
    assert "No files were changed" in result["error"]
    assert summary.tasks == []
    assert (mock_usb / "Contents" / "song.flac").exists()


def test_experimental_onelibrary_bridge_retains_originals_and_database(mock_usb: Path):
    dlp = mock_usb / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    dlp.parent.mkdir(parents=True)
    original_dlp = b"encrypted OneLibrary database"
    dlp.write_bytes(original_dlp)
    engine = ConversionEngine()
    summary = engine.scan(mock_usb, allow_onelibrary_bridge=True)

    assert summary.onelibrary_bridge_mode is True
    assert summary.unsupported_reason is None
    assert len(summary.tasks) == 1

    unauthorized_result = engine.execute(
        summary,
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
    )
    assert unauthorized_result["success"] is False
    assert "old audio paths" in unauthorized_result["error"]

    unsafe_result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        allow_onelibrary_bridge=True,
    )
    assert unsafe_result["success"] is False
    assert "retaining all original" in unsafe_result["error"]

    no_backup_result = engine.execute(
        summary,
        delete_original=False,
        backup=False,
        clean_dotfiles=False,
        allow_onelibrary_bridge=True,
    )
    assert no_backup_result["success"] is False
    assert "requires database backups" in no_backup_result["error"]

    result = engine.execute(
        summary,
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        allow_onelibrary_bridge=True,
    )

    assert result["success"] is True
    assert result["onelibrary_sync_required"] is True
    assert "STEP 1 OF 2 IS COMPLETE" in result["warnings"][0]
    assert (mock_usb / "Contents" / "song.flac").exists()
    assert (mock_usb / "Contents" / "song.aiff").exists()
    assert dlp.read_bytes() == original_dlp
    assert PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb").tracks[
        0
    ].file_path.endswith(".aiff")


def test_scan_uses_short_aif_extension_when_aiff_does_not_fit(tmp_path: Path):
    usb = tmp_path / "USB"
    contents = usb / "Contents"
    rekordbox = usb / "PIONEER" / "rekordbox"
    anlz_dir = usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    dlp = usb / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    contents.mkdir(parents=True)
    rekordbox.mkdir(parents=True)
    anlz_dir.mkdir(parents=True)
    dlp.parent.mkdir(parents=True)

    source = contents / "song.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.05",
            "-ar", "44100", "-c:a", "pcm_s32le", str(source),
        ],
        check=True,
    )
    pdb = create_minimal_pdb(
        rekordbox,
        file_size=source.stat().st_size,
        filename="song.wav",
        filepath="/Contents/song.wav",
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
    )
    data = bytearray(pdb.read_bytes())
    struct.pack_into("<H", data, 4096 + 0x28 + 0x52, 32)
    pdb.write_bytes(data)
    create_minimal_anlz(anlz_dir, "/Contents/song.wav")
    dlp.write_bytes(b"unchanged OneLibrary")

    engine = ConversionEngine()
    summary = engine.scan(
        usb,
        profile=get_profile(CompatibilityProfileType.MODERN),
        forced_target_format=TargetFormat.AIFF,
        allow_onelibrary_bridge=True,
    )

    assert len(summary.tasks) == 1
    assert summary.tasks[0].target_filename == "song.aif"
    assert summary.tasks[0].target_usb_path == "/Contents/song.aif"

    result = engine.execute(
        summary,
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        allow_onelibrary_bridge=True,
    )

    assert result["success"] is True
    assert source.exists()
    assert (contents / "song.aif").is_file()
    assert dlp.read_bytes() == b"unchanged OneLibrary"
    assert PDBManager(pdb).tracks[0].file_path == "/Contents/song.aif"


def test_restore_succeeds_when_originals_were_retained(mock_usb: Path):
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
    )
    assert result["success"] is True

    restored, message = engine.restore_backup(mock_usb)

    assert restored is True, message
    assert PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb").tracks[0].file_path.endswith(".flac")


def test_restore_ignores_unrelated_stale_anlz_backup(mock_usb: Path, tmp_path: Path):
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
    )
    assert result["success"] is True
    unrelated_dir = mock_usb / "PIONEER" / "USBANLZ" / "UNRELATED"
    unrelated_dir.mkdir()
    unrelated = create_minimal_anlz(unrelated_dir, "/Contents/current.aiff")
    old_backup = create_minimal_anlz(tmp_path, "/Contents/stale.flac")
    unrelated.with_suffix(".DAT.bak").write_bytes(old_backup.read_bytes())

    restored, message = engine.restore_backup(mock_usb)

    assert restored is True, message
    assert ANLZManager.read_path(unrelated) == "/Contents/current.aiff"


def test_restore_rolls_back_sidecars_if_database_restore_fails(
    mock_usb: Path, monkeypatch
):
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
    )
    assert result["success"] is True
    original_restore = engine._restore_file_bytes

    def fail_database_restore(path: Path, data: bytes):
        if path.name == "export.pdb":
            raise OSError("simulated write failure")
        original_restore(path, data)

    monkeypatch.setattr(engine, "_restore_file_bytes", fail_database_restore)

    restored, message = engine.restore_backup(mock_usb)

    assert restored is False
    assert "rolled back" in message
    assert PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb").tracks[0].file_path.endswith(".aiff")
    assert ANLZManager.read_path(
        mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001" / "ANLZ0000.DAT"
    ) == "/Contents/song.aiff"


def test_engine_rejects_path_that_escapes_usb(tmp_path: Path):
    usb = tmp_path / "USB"
    rekordbox = usb / "PIONEER" / "rekordbox"
    rekordbox.mkdir(parents=True)
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"must remain")
    create_minimal_pdb(
        rekordbox,
        file_size=outside.stat().st_size,
        filename="outside.flac",
        filepath="/../outside.flac",
    )
    engine = ConversionEngine()

    result = engine.execute(engine.scan(usb), clean_dotfiles=False)

    assert result["success"] is False
    assert "escapes the USB root" in result["preflight_errors"][0]
    assert outside.read_bytes() == b"must remain"


def test_engine_rejects_anlz_path_that_escapes_usb(mock_usb: Path, tmp_path: Path):
    source = mock_usb / "Contents" / "song.flac"
    create_minimal_pdb(
        mock_usb / "PIONEER" / "rekordbox",
        file_size=source.stat().st_size,
        analyze_path="/../outside.DAT",
    )
    outside = tmp_path / "outside.DAT"
    outside.write_bytes(b"must remain")
    engine = ConversionEngine()

    result = engine.execute(engine.scan(mock_usb), clean_dotfiles=False)

    assert result["success"] is False
    assert "Unsafe ANLZ path" in result["preflight_errors"][0]
    assert outside.read_bytes() == b"must remain"


def test_engine_rejects_stale_scan_plan(mock_usb: Path):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)
    pdb = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    changed = bytearray(pdb.read_bytes())
    changed[0x14] ^= 1
    pdb.write_bytes(changed)

    result = engine.execute(summary, clean_dotfiles=False)

    assert result["success"] is False
    assert "changed after the scan" in result["error"]
    assert not (mock_usb / "Contents" / "song.aiff").exists()


def test_engine_refuses_insufficient_staging_space(mock_usb: Path, monkeypatch):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)
    disk_usage = type("DiskUsage", (), {"free": 1})()
    monkeypatch.setattr(
        "rekordbox_compatibility_converter.core.engine.shutil.disk_usage",
        lambda _path: disk_usage,
    )

    result = engine.execute(summary, clean_dotfiles=False)

    assert result["success"] is False
    assert "Insufficient free space" in result["error"]
    assert not (mock_usb / "Contents" / "song.aiff").exists()


def test_engine_refuses_mismatched_database_filename(mock_usb: Path):
    source = mock_usb / "Contents" / "song.flac"
    create_minimal_pdb(
        mock_usb / "PIONEER" / "rekordbox",
        file_size=source.stat().st_size,
        filename="different.flac",
        filepath="/Contents/song.flac",
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
    )
    engine = ConversionEngine()

    result = engine.execute(engine.scan(mock_usb), clean_dotfiles=False)

    assert result["success"] is False
    assert "filename and file path disagree" in result["preflight_errors"][0]
    assert source.exists()


def test_scan_keeps_dotted_directory_when_filename_has_no_extension(tmp_path: Path):
    usb = tmp_path / "USB"
    rekordbox = usb / "PIONEER" / "rekordbox"
    rekordbox.mkdir(parents=True)
    create_minimal_pdb(
        rekordbox,
        filename="song",
        filepath="/Contents/Artist.Name/song",
    )

    summary = ConversionEngine().scan(usb)

    assert summary.tasks[0].target_usb_path == "/Contents/Artist.Name/song.aiff"


def test_keep_originals_refuses_same_path_conversion(tmp_path: Path):
    usb = tmp_path / "USB"
    contents = usb / "Contents"
    rekordbox = usb / "PIONEER" / "rekordbox"
    contents.mkdir(parents=True)
    rekordbox.mkdir(parents=True)
    audio = contents / "song.aiff"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.05",
            "-ar", "96000", "-c:a", "pcm_s24be", str(audio),
        ],
        check=True,
    )
    pdb = create_minimal_pdb(
        rekordbox,
        file_size=audio.stat().st_size,
        filename="song.aiff",
        filepath="/Contents/song.aiff",
    )
    data = bytearray(pdb.read_bytes())
    struct.pack_into("<I", data, 4096 + 0x28 + 0x08, 96000)
    struct.pack_into("<H", data, 4096 + 0x28 + 0x52, 24)
    pdb.write_bytes(data)
    digest_before = hashlib.sha256(audio.read_bytes()).digest()
    engine = ConversionEngine()

    result = engine.execute(
        engine.scan(usb), delete_original=False, backup=False, clean_dotfiles=False
    )

    assert result["success"] is False
    assert "Cannot keep the original" in result["preflight_errors"][0]
    assert hashlib.sha256(audio.read_bytes()).digest() == digest_before
