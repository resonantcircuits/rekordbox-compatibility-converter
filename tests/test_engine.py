"""End-to-end integration tests for ConversionEngine with multi-threading, dotfile cleanup, and restore."""

import subprocess
import hashlib
import inspect
import json
import os
import struct
from pathlib import Path
import pytest
from rekordbox_compatibility_converter.core.engine import (
    DEFAULT_CONVERSION_THREADS,
    ConversionEngine,
)
from rekordbox_compatibility_converter.core.local_backup import LocalBackupSession
from rekordbox_compatibility_converter.core.anlz_manager import ANLZManager
from rekordbox_compatibility_converter.core.models import (
    CompatibilityProfileType,
    REKORDBOX_FILE_TYPE_BY_TARGET,
    TargetFormat,
    TrackInfo,
)
from rekordbox_compatibility_converter.core.pdb_manager import (
    DATABASE_SEQUENCE_OFFSET,
    PAGE_SEQUENCE_OFFSET,
    PDBManager,
    TRACK_FILE_TYPE_OFFSET,
)
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

    # Create all waveform generations exported by current Rekordbox versions.
    anlz_dat = create_minimal_anlz(anlz_dir, "/Contents/song.flac")
    anlz_ext = anlz_dat.with_suffix(".EXT")
    anlz_2ex = anlz_dat.with_suffix(".2EX")
    anlz_ext.write_bytes(anlz_dat.read_bytes())
    anlz_2ex.write_bytes(anlz_dat.read_bytes())

    return usb_dir


def test_default_conversion_threads_are_usb_safe():
    default = inspect.signature(ConversionEngine.execute).parameters["threads"].default

    assert DEFAULT_CONVERSION_THREADS == 2
    assert default == DEFAULT_CONVERSION_THREADS


def test_scan_enforces_16_bit_across_otherwise_compatible_lossless_audio(
    mock_usb: Path
):
    contents = mock_usb / "Contents"
    (contents / "song.flac").unlink()
    source = contents / "song.aiff"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.5",
            "-ar", "44100", "-c:a", "pcm_s24be", str(source),
        ],
        check=True,
    )
    pdb = create_minimal_pdb(
        mock_usb / "PIONEER" / "rekordbox",
        file_size=source.stat().st_size,
        filename="song.aiff",
        filepath="/Contents/song.aiff",
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
        file_type=0x0C,
    )
    data = bytearray(pdb.read_bytes())
    struct.pack_into("<H", data, 4096 + 0x28 + 0x52, 24)
    pdb.write_bytes(data)
    engine = ConversionEngine()

    profile_default = engine.scan(mock_usb)
    enforced = engine.scan(mock_usb, enforce_pcm_16_bit=True)

    assert profile_default.tasks == []
    assert len(enforced.tasks) == 1
    assert enforced.tasks[0].source_abs_path == source
    assert enforced.tasks[0].target_abs_path == source
    assert enforced.tasks[0].target_sample_depth == 16

    result = engine.execute(
        enforced,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=mock_usb.parent / "Local Backups",
    )

    assert result["success"] is True
    assert engine.audio_converter.probe(source)["bits_per_sample"] == 16
    restored, message = engine.restore_local_backup(
        Path(result["local_backup_session"]), mock_usb
    )
    assert restored is True, message
    assert engine.audio_converter.probe(source)["bits_per_sample"] == 24


def test_engine_scan_and_execute_parallel(mock_usb: Path):
    engine = ConversionEngine()
    profile = get_profile(CompatibilityProfileType.STANDARD)
    pdb_path = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    original_pdb = pdb_path.read_bytes()
    database_sequence_before = struct.unpack_from(
        "<I", original_pdb, DATABASE_SEQUENCE_OFFSET
    )[0]
    page_sequence_before = struct.unpack_from(
        "<I", original_pdb, 4096 + PAGE_SEQUENCE_OFFSET
    )[0]

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
    assert summary.required_space_bytes > summary.estimated_extra_bytes

    task = summary.tasks[0]
    assert task.target_filename == "song.aiff"
    assert task.target_usb_path == "/Contents/song.aiff"

    # Execute conversion with 2 parallel workers and dotfile cleanup
    result = engine.execute(summary=summary, delete_original=True, backup=True, threads=2, clean_dotfiles=True)
    assert result.get("success") is True
    assert result.get("completed") == 1
    assert result.get("anlz_updated") == 3
    assert result.get("cleaned_dotfiles") >= 1
    anlz_dir = mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    assert (anlz_dir / "ANLZ0000.DAT.bak").is_file()
    assert (anlz_dir / "ANLZ0000.EXT.bak").is_file()
    assert (anlz_dir / "ANLZ0000.2EX.bak").is_file()

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
    assert ANLZManager.read_path(
        mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001" / "ANLZ0000.EXT"
    ) == "/Contents/song.aiff"
    assert ANLZManager.read_path(
        mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001" / "ANLZ0000.2EX"
    ) == "/Contents/song.aiff"

    validation = ExportValidator().validate(mock_usb, profile)
    assert validation.failed_tracks == 0
    assert validation.issues == []
    converted_pdb = pdb_path.read_bytes()
    assert (
        struct.unpack_from("<I", converted_pdb, DATABASE_SEQUENCE_OFFSET)[0]
        == database_sequence_before
    )
    assert (
        struct.unpack_from("<I", converted_pdb, 4096 + PAGE_SEQUENCE_OFFSET)[0]
        == page_sequence_before
    )

    # A database-only restore is refused after the original audio was deleted.
    success, msg = engine.restore_backup(mock_usb)
    assert success is False
    assert "missing or changed" in msg
    assert pdb_path.exists()


@pytest.mark.parametrize(
    ("target_format", "target_extension", "expected_bitrate"),
    [
        (TargetFormat.AIFF, "aiff", 705),
        (TargetFormat.WAV, "wav", 705),
        (TargetFormat.MP3, "mp3", 320),
    ],
)
def test_engine_updates_device_sql_file_type_for_every_target(
    mock_usb: Path,
    target_format: TargetFormat,
    target_extension: str,
    expected_bitrate: int,
):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb, forced_target_format=target_format)

    result = engine.execute(
        summary,
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
    )

    assert result["success"] is True
    track = PDBManager(
        mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    ).tracks[0]
    assert track.extension == target_extension
    assert track.file_type == REKORDBOX_FILE_TYPE_BY_TARGET[target_format]
    assert track.bitrate == expected_bitrate


def test_scan_repairs_v030_bitrate_units_without_touching_audio_or_waveforms(
    mock_usb: Path,
):
    engine = ConversionEngine()
    initial = engine.scan(mock_usb, forced_target_format=TargetFormat.AIFF)
    converted = engine.execute(
        initial,
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
    )
    assert converted["success"] is True

    pdb_path = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    manager = PDBManager(pdb_path)
    track = manager.tracks[0]
    assert manager.update_track(
        track=track,
        new_filename=track.filename,
        new_filepath=track.file_path,
        new_filesize=track.file_size,
        new_sample_rate=track.sample_rate,
        new_sample_depth=track.sample_depth,
        new_bitrate=705600,
        new_file_type=track.file_type,
    )
    manager.save(backup=False)
    audio_path = mock_usb / track.file_path.lstrip("/")
    anlz_path = mock_usb / track.analyze_path.lstrip("/")
    audio_before = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    anlz_before = hashlib.sha256(anlz_path.read_bytes()).hexdigest()

    repair_plan = engine.scan(mock_usb)

    assert repair_plan.tasks == []
    assert repair_plan.analysis_repairs == []
    assert len(repair_plan.bitrate_repairs) == 1
    assert repair_plan.bitrate_repairs[0].old_bitrate == 705600
    assert repair_plan.bitrate_repairs[0].new_bitrate == 705

    repaired = engine.execute(repair_plan, clean_dotfiles=False)

    assert repaired["success"] is True
    assert repaired["completed"] == 0
    assert repaired["bitrate_metadata_repaired"] == 1
    assert PDBManager(pdb_path).tracks[0].bitrate == 705
    assert hashlib.sha256(audio_path.read_bytes()).hexdigest() == audio_before
    assert hashlib.sha256(anlz_path.read_bytes()).hexdigest() == anlz_before


def test_scan_repairs_legacy_mp3_bitrate_despite_container_average_variance(
    mock_usb: Path, monkeypatch
):
    engine = ConversionEngine()
    converted = engine.execute(
        engine.scan(mock_usb, forced_target_format=TargetFormat.MP3),
        delete_original=False,
        backup=False,
        clean_dotfiles=False,
        threads=1,
    )
    assert converted["success"] is True
    pdb_path = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    manager = PDBManager(pdb_path)
    track = manager.tracks[0]
    assert manager.update_track_bitrate(track, 320000)
    manager.save(backup=False)
    real_probe = engine.audio_converter.probe

    def variable_average_probe(path):
        probe = real_probe(path)
        if Path(path).suffix.lower() == ".mp3":
            probe["bit_rate"] = 323000
        return probe

    monkeypatch.setattr(engine.audio_converter, "probe", variable_average_probe)

    repair_plan = engine.scan(mock_usb)

    assert repair_plan.tasks == []
    assert len(repair_plan.bitrate_repairs) == 1
    assert repair_plan.bitrate_repairs[0].old_bitrate == 320000
    assert repair_plan.bitrate_repairs[0].new_bitrate == 320


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
    restored_track = PDBManager(
        mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    ).tracks[0]
    assert restored_track.file_path.endswith(".flac")
    assert restored_track.file_type == 0x05


def test_engine_rolls_back_dat_and_ext_when_2ex_update_fails(
    mock_usb: Path, monkeypatch
):
    original_update = ANLZManager.update_path

    def fail_2ex(path: Path, *args, **kwargs):
        if path.suffix == ".2EX":
            return False
        return original_update(path, *args, **kwargs)

    monkeypatch.setattr(ANLZManager, "update_path", staticmethod(fail_2ex))
    engine = ConversionEngine()

    result = engine.execute(
        engine.scan(mock_usb), backup=False, clean_dotfiles=False, threads=1
    )

    assert result["success"] is False
    assert (mock_usb / "Contents" / "song.flac").is_file()
    assert not (mock_usb / "Contents" / "song.aiff").exists()
    assert PDBManager(
        mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    ).tracks[0].file_path == "/Contents/song.flac"
    anlz_dir = mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    for suffix in (".DAT", ".EXT", ".2EX"):
        assert ANLZManager.read_path(anlz_dir / f"ANLZ0000{suffix}") == (
            "/Contents/song.flac"
        )


def test_engine_preflight_rejects_stale_2ex_path(
    mock_usb: Path, tmp_path: Path
):
    stale = create_minimal_anlz(tmp_path, "/Contents/stale.flac").read_bytes()
    anlz_2ex = (
        mock_usb
        / "PIONEER"
        / "USBANLZ"
        / "P001"
        / "00000001"
        / "ANLZ0000.2EX"
    )
    anlz_2ex.write_bytes(stale)
    engine = ConversionEngine()

    result = engine.execute(engine.scan(mock_usb), clean_dotfiles=False)

    assert result["success"] is False
    assert any("ANLZ .2EX" in error for error in result["preflight_errors"])
    assert not (mock_usb / "Contents" / "song.aiff").exists()


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


def test_onelibrary_bridge_archives_originals_locally_and_reclaims_usb_space(
    mock_usb: Path, tmp_path: Path
):
    dlp = mock_usb / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    dlp.parent.mkdir(parents=True)
    original_dlp = b"encrypted OneLibrary database"
    dlp.write_bytes(original_dlp)
    backup_base = tmp_path / "Local Backups"
    engine = ConversionEngine()
    summary = engine.scan(mock_usb, allow_onelibrary_bridge=True)
    phases = []

    assert summary.required_space_with_local_backup_bytes < summary.required_space_bytes
    assert summary.local_backup_required_space_bytes > 0

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        allow_onelibrary_bridge=True,
        local_original_backup_dir=backup_base,
        phase_callback=lambda phase, current, total, detail: phases.append(
            (phase, current, total, detail)
        ),
    )

    assert result["success"] is True
    assert result["onelibrary_sync_required"] is True
    assert not (mock_usb / "Contents" / "song.flac").exists()
    assert (mock_usb / "Contents" / "song.aiff").is_file()
    assert dlp.read_bytes() == original_dlp
    assert not (mock_usb / "PIONEER" / "rekordbox" / "export.pdb.bak").exists()
    session = Path(result["local_backup_session"])
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["originals"][0]["status"] == "converted"
    assert (session / manifest["originals"][0]["archive_path"]).is_file()
    assert any(item["usb_path"].endswith("export.pdb") for item in manifest["metadata"])
    phase_names = [event[0] for event in phases]
    assert phase_names[0] == "preflight"
    assert "backup" in phase_names
    assert "backup_verification" in phase_names
    assert "conversion" in phase_names
    assert phase_names[-1] == "finalizing"
    for phase in ("backup", "backup_verification"):
        final_event = [event for event in phases if event[0] == phase][-1]
        assert final_event[1] == final_event[2]
        assert final_event[3]


def test_local_archive_conversion_failure_restores_usb_original(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)
    monkeypatch.setattr(
        engine.audio_converter,
        "convert",
        lambda **_kwargs: (False, 0, "simulated conversion failure"),
    )

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
    )

    assert result["success"] is False
    assert (mock_usb / "Contents" / "song.flac").is_file()
    assert not (mock_usb / "Contents" / "song.aiff").exists()
    manifest = json.loads(
        (Path(result["local_backup_session"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "complete_with_errors"
    assert manifest["originals"][0]["status"] == "restored_after_failure"


def test_local_archive_requires_confirmation_before_replacing_existing_target(
    mock_usb: Path, tmp_path: Path
):
    existing_target = mock_usb / "Contents" / "song.aiff"
    old_target = b"pre-existing unreferenced target"
    existing_target.write_bytes(old_target)
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
    )

    assert result["success"] is False
    assert "explicit reuse or replacement" in result["preflight_errors"][0]
    assert existing_target.read_bytes() == old_target
    assert (mock_usb / "Contents" / "song.flac").is_file()


def test_local_archive_replaces_and_can_restore_preexisting_target(
    mock_usb: Path, tmp_path: Path
):
    existing_target = mock_usb / "Contents" / "song.aiff"
    old_target = b"pre-existing unreferenced target"
    existing_target.write_bytes(old_target)
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
        replace_existing_targets=True,
    )

    assert result["success"] is True
    assert existing_target.read_bytes() != old_target
    session = Path(result["local_backup_session"])
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["preexisting_targets"]) == 1
    archived_target = session / manifest["preexisting_targets"][0]["archive_path"]
    assert archived_target.read_bytes() == old_target

    restored, message = engine.restore_local_backup(session, mock_usb)

    assert restored is True, message
    assert existing_target.read_bytes() == old_target
    assert (mock_usb / "Contents" / "song.flac").is_file()


def test_local_archive_conversion_failure_restores_preexisting_target(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    existing_target = mock_usb / "Contents" / "song.aiff"
    old_target = b"pre-existing unreferenced target"
    existing_target.write_bytes(old_target)
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)
    monkeypatch.setattr(
        engine.audio_converter,
        "convert",
        lambda **_kwargs: (False, 0, "simulated conversion failure"),
    )

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
        replace_existing_targets=True,
    )

    assert result["success"] is False
    assert existing_target.read_bytes() == old_target
    assert (mock_usb / "Contents" / "song.flac").is_file()


def test_local_archive_audio_verifies_and_reuses_referenced_target(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    engine = ConversionEngine()
    source = mock_usb / "Contents" / "song.flac"
    target = mock_usb / "Contents" / "song.aiff"
    success, _size, error = engine.audio_converter.convert(source, target)
    assert success is True, error
    target_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    summary = engine.scan(mock_usb)

    pdb_manager = PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb")
    pdb_manager.tracks.append(
        TrackInfo(
            id=202,
            filename="song.aiff",
            file_path="/Contents/song.aiff",
            file_type=REKORDBOX_FILE_TYPE_BY_TARGET[TargetFormat.AIFF],
        )
    )
    monkeypatch.setattr(
        "rekordbox_compatibility_converter.core.engine.PDBManager",
        lambda _path: pdb_manager,
    )

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
        replace_existing_targets=True,
    )

    assert result["success"] is True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == target_digest
    session = Path(result["local_backup_session"])
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["preexisting_targets"] == []
    assert manifest["originals"][0]["converted_target_preexisting"] is True

    restored, message = engine.restore_local_backup(session, mock_usb)

    assert restored is True, message
    assert source.is_file()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == target_digest


def test_missing_original_adopts_strictly_matching_referenced_target(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    engine = ConversionEngine()
    source = mock_usb / "Contents" / "song.flac"
    target = mock_usb / "Contents" / "song.mp3"
    success, _size, error = engine.audio_converter.convert(
        source,
        target,
        target_format=TargetFormat.MP3,
        sample_rate=44100,
        sample_depth=16,
    )
    assert success is True, error
    target_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    summary = engine.scan(mock_usb, forced_target_format=TargetFormat.MP3)
    task = summary.tasks[0]
    task.track.duration = round(engine.audio_converter.probe(target)["duration"])
    pdb_manager = PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb")
    pdb_manager.tracks.append(
        TrackInfo(
            id=202,
            title=task.track.title,
            filename=task.target_filename,
            file_path=task.target_usb_path,
            analyze_path=task.track.analyze_path,
            sample_rate=task.target_sample_rate,
            sample_depth=task.target_sample_depth,
            bitrate=320,
            file_size=target.stat().st_size,
            file_type=REKORDBOX_FILE_TYPE_BY_TARGET[TargetFormat.MP3],
            duration=task.track.duration,
        )
    )
    monkeypatch.setattr(
        "rekordbox_compatibility_converter.core.engine.PDBManager",
        lambda _path: pdb_manager,
    )
    source.unlink()

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
        replace_existing_targets=True,
    )

    assert result["success"] is True, result.get("preflight_errors")
    assert result["completed"] == 1
    assert result["adopted_existing_targets"] == 1
    assert task.adopt_existing_target is True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == target_digest
    assert task.track.file_path == "/Contents/song.mp3"
    assert PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb").tracks[
        0
    ].file_path == "/Contents/song.mp3"
    assert ANLZManager.read_path(task.anlz_dat_path) == "/Contents/song.mp3"
    session = Path(result["local_backup_session"])
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["originals"] == []


def test_missing_original_refuses_mismatched_referenced_target(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    engine = ConversionEngine()
    source = mock_usb / "Contents" / "song.flac"
    target = mock_usb / "Contents" / "song.mp3"
    success, _size, error = engine.audio_converter.convert(
        source,
        target,
        target_format=TargetFormat.MP3,
    )
    assert success is True, error
    summary = engine.scan(mock_usb, forced_target_format=TargetFormat.MP3)
    task = summary.tasks[0]
    pdb_manager = PDBManager(mock_usb / "PIONEER" / "rekordbox" / "export.pdb")
    pdb_manager.tracks.append(
        TrackInfo(
            id=202,
            title="Different track",
            filename=task.target_filename,
            file_path=task.target_usb_path,
            analyze_path=task.track.analyze_path,
            sample_rate=task.target_sample_rate,
            sample_depth=task.target_sample_depth,
            file_size=target.stat().st_size,
            file_type=REKORDBOX_FILE_TYPE_BY_TARGET[TargetFormat.MP3],
            duration=task.track.duration,
        )
    )
    monkeypatch.setattr(
        "rekordbox_compatibility_converter.core.engine.PDBManager",
        lambda _path: pdb_manager,
    )
    source.unlink()

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        local_original_backup_dir=tmp_path / "Local Backups",
        replace_existing_targets=True,
    )

    assert result["success"] is False
    assert "track title differs" in result["preflight_errors"][0]
    assert pdb_manager.tracks[0].file_path == "/Contents/song.flac"
    assert target.is_file()


def test_compatible_track_repairs_extension_only_stale_waveform_paths(
    mock_usb: Path, tmp_path: Path
):
    engine = ConversionEngine()
    flac = mock_usb / "Contents" / "song.flac"
    mp3 = mock_usb / "Contents" / "song.mp3"
    success, _size, error = engine.audio_converter.convert(
        flac, mp3, target_format=TargetFormat.MP3
    )
    assert success is True, error
    flac.unlink()
    create_minimal_pdb(
        mock_usb / "PIONEER" / "rekordbox",
        file_size=mp3.stat().st_size,
        filename="song.mp3",
        filepath="/Contents/song.mp3",
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
        file_type=0x01,
    )

    summary = engine.scan(mock_usb)

    assert summary.tasks == []
    assert len(summary.analysis_repairs) == 1
    repair = summary.analysis_repairs[0]
    assert repair.old_audio_path == "/Contents/song.flac"
    assert repair.new_audio_path == "/Contents/song.mp3"

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        local_original_backup_dir=tmp_path / "Local Backups",
    )

    assert result["success"] is True, result
    assert result["completed"] == 0
    assert result["analysis_paths_repaired"] == 1
    assert mp3.is_file()
    assert all(
        ANLZManager.read_path(sidecar) == "/Contents/song.mp3"
        for sidecar in repair.sidecar_paths
    )
    session = Path(result["local_backup_session"])
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["originals"] == []
    assert len(manifest["metadata"]) >= 4


def test_compatible_track_repairs_only_divergent_stale_2ex(
    mock_usb: Path, tmp_path: Path
):
    engine = ConversionEngine()
    flac = mock_usb / "Contents" / "song.flac"
    mp3 = mock_usb / "Contents" / "song.mp3"
    success, _size, error = engine.audio_converter.convert(
        flac, mp3, target_format=TargetFormat.MP3
    )
    assert success is True, error
    flac.unlink()
    create_minimal_pdb(
        mock_usb / "PIONEER" / "rekordbox",
        file_size=mp3.stat().st_size,
        filename="song.mp3",
        filepath="/Contents/song.mp3",
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
        file_type=0x01,
        bitrate=320,
    )
    anlz_dir = mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    current_dir = tmp_path / "current"
    stale_dir = tmp_path / "stale"
    current_dir.mkdir()
    stale_dir.mkdir()
    current = create_minimal_anlz(current_dir, "/Contents/song.mp3").read_bytes()
    stale = create_minimal_anlz(stale_dir, "/Contents/song.flac").read_bytes()
    (anlz_dir / "ANLZ0000.DAT").write_bytes(current)
    (anlz_dir / "ANLZ0000.EXT").write_bytes(current)
    (anlz_dir / "ANLZ0000.2EX").write_bytes(stale)

    summary = engine.scan(mock_usb)

    assert summary.tasks == []
    assert len(summary.analysis_repairs) == 1
    repair = summary.analysis_repairs[0]
    assert repair.old_audio_path == "/Contents/song.flac"
    assert repair.sidecar_paths == [anlz_dir / "ANLZ0000.2EX"]

    result = engine.execute(summary, backup=False, clean_dotfiles=False)

    assert result["success"] is True, result
    assert result["analysis_paths_repaired"] == 1
    assert all(
        ANLZManager.read_path(anlz_dir / f"ANLZ0000{suffix}")
        == "/Contents/song.mp3"
        for suffix in (".DAT", ".EXT", ".2EX")
    )


def test_stale_waveform_path_with_different_stem_is_not_auto_repaired(mock_usb: Path):
    engine = ConversionEngine()
    flac = mock_usb / "Contents" / "song.flac"
    mp3 = mock_usb / "Contents" / "different.mp3"
    success, _size, error = engine.audio_converter.convert(
        flac, mp3, target_format=TargetFormat.MP3
    )
    assert success is True, error
    flac.unlink()
    create_minimal_pdb(
        mock_usb / "PIONEER" / "rekordbox",
        file_size=mp3.stat().st_size,
        filename="different.mp3",
        filepath="/Contents/different.mp3",
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
        file_type=0x01,
    )

    summary = engine.scan(mock_usb)

    assert summary.tasks == []
    assert summary.analysis_repairs == []


def test_waveform_path_repair_rolls_back_every_sidecar_on_failure(
    mock_usb: Path, monkeypatch
):
    engine = ConversionEngine()
    flac = mock_usb / "Contents" / "song.flac"
    mp3 = mock_usb / "Contents" / "song.mp3"
    success, _size, error = engine.audio_converter.convert(
        flac, mp3, target_format=TargetFormat.MP3
    )
    assert success is True, error
    flac.unlink()
    create_minimal_pdb(
        mock_usb / "PIONEER" / "rekordbox",
        file_size=mp3.stat().st_size,
        filename="song.mp3",
        filepath="/Contents/song.mp3",
        analyze_path="/PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
        file_type=0x01,
    )
    summary = engine.scan(mock_usb)
    repair = summary.analysis_repairs[0]
    original_bytes = {path: path.read_bytes() for path in repair.sidecar_paths}
    original_update_path = ANLZManager.update_path
    calls = 0

    def fail_second_update(path, new_path, backup=True):
        nonlocal calls
        calls += 1
        if calls == 2:
            return False
        return original_update_path(path, new_path, backup=backup)

    monkeypatch.setattr(ANLZManager, "update_path", fail_second_update)

    result = engine.execute(
        summary,
        delete_original=True,
        backup=False,
        clean_dotfiles=False,
    )

    assert result["success"] is False
    assert result["analysis_paths_repaired"] == 0
    assert result["failed"] == 1
    assert all(path.read_bytes() == data for path, data in original_bytes.items())


def test_local_archive_failure_does_not_modify_usb(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated local disk failure")

    monkeypatch.setattr(LocalBackupSession, "_copy_verified", fail_copy)
    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        local_original_backup_dir=tmp_path / "Local Backups",
    )

    assert result["success"] is False
    assert "verified local original backup" in result["error"]
    assert (mock_usb / "Contents" / "song.flac").is_file()
    assert not (mock_usb / "Contents" / "song.aiff").exists()


def test_local_archive_folder_cannot_be_inside_usb(mock_usb: Path):
    result = ConversionEngine().execute(
        ConversionEngine().scan(mock_usb),
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        local_original_backup_dir=mock_usb / "Backups",
    )

    assert result["success"] is False
    assert "cannot be located on the selected USB" in result["error"]
    assert (mock_usb / "Contents" / "song.flac").is_file()
    assert not (mock_usb / "Backups").exists()


def test_restore_local_archive_recovers_audio_database_and_waveforms(
    mock_usb: Path, tmp_path: Path
):
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
    )
    session = Path(result["local_backup_session"])

    restored, message = engine.restore_local_backup(session, mock_usb)

    assert restored is True, message
    assert (mock_usb / "Contents" / "song.flac").is_file()
    assert not (mock_usb / "Contents" / "song.aiff").exists()
    assert PDBManager(
        mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    ).tracks[0].file_path == "/Contents/song.flac"
    anlz_dir = mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    for suffix in (".DAT", ".EXT", ".2EX"):
        assert ANLZManager.read_path(anlz_dir / f"ANLZ0000{suffix}") == (
            "/Contents/song.flac"
        )
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "restored"


def test_restore_local_archive_rolls_back_every_file_on_metadata_failure(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
    )
    session = Path(result["local_backup_session"])
    converted = mock_usb / "Contents" / "song.aiff"
    converted_digest = hashlib.sha256(converted.read_bytes()).hexdigest()
    pdb_path = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    pdb_digest = hashlib.sha256(pdb_path.read_bytes()).hexdigest()
    anlz_dir = mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    analysis_digests = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            anlz_dir / "ANLZ0000.DAT",
            anlz_dir / "ANLZ0000.EXT",
            anlz_dir / "ANLZ0000.2EX",
        )
    }
    original_copy = LocalBackupSession._copy_verified
    failed_once = False

    def fail_first_database_restore(
        backup_session, source, destination, progress_callback=None
    ):
        nonlocal failed_once
        if Path(destination).name == "export.pdb" and not failed_once:
            failed_once = True
            raise OSError("injected database restore failure")
        return original_copy(
            backup_session, source, destination, progress_callback
        )

    monkeypatch.setattr(
        LocalBackupSession, "_copy_verified", fail_first_database_restore
    )

    restored, message = engine.restore_local_backup(session, mock_usb)

    assert restored is False
    assert "prior USB state was restored" in message
    assert failed_once is True
    assert not (mock_usb / "Contents" / "song.flac").exists()
    assert hashlib.sha256(converted.read_bytes()).hexdigest() == converted_digest
    assert hashlib.sha256(pdb_path.read_bytes()).hexdigest() == pdb_digest
    assert PDBManager(pdb_path).tracks[0].file_path == "/Contents/song.aiff"
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in analysis_digests.items()
    )


def test_restore_local_archive_refuses_changed_converted_file(
    mock_usb: Path, tmp_path: Path
):
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=tmp_path / "Local Backups",
    )
    converted = mock_usb / "Contents" / "song.aiff"
    converted.write_bytes(converted.read_bytes() + b"changed")

    restored, message = engine.restore_local_backup(
        Path(result["local_backup_session"]), mock_usb
    )

    assert restored is False
    assert "changed after this backup" in message
    assert converted.is_file()
    assert not (mock_usb / "Contents" / "song.flac").exists()


def test_local_archive_allows_usb_space_below_legacy_staging_requirement(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)
    backup_base = tmp_path / "Local Backups"
    backup_base.mkdir()
    reduced_required = summary.required_space_with_local_backup_bytes
    assert reduced_required < summary.required_space_bytes
    simulated_usb_free = reduced_required

    def disk_usage(path):
        resolved = Path(path).resolve()
        free = 10 * 1024 ** 3 if resolved == backup_base.resolve() else simulated_usb_free
        return type("DiskUsage", (), {"free": free})()

    monkeypatch.setattr(
        "rekordbox_compatibility_converter.core.engine.shutil.disk_usage",
        disk_usage,
    )

    result = engine.execute(
        summary,
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        local_original_backup_dir=backup_base,
    )

    assert result["success"] is True
    assert (mock_usb / "Contents" / "song.aiff").is_file()
    assert not (mock_usb / "Contents" / "song.flac").exists()


def test_local_archive_refuses_insufficient_computer_space_without_usb_changes(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    backup_base = tmp_path / "Local Backups"
    backup_base.mkdir()

    def disk_usage(path):
        free = 1 if Path(path).resolve() == backup_base.resolve() else 10 * 1024 ** 3
        return type("DiskUsage", (), {"free": free})()

    monkeypatch.setattr(
        "rekordbox_compatibility_converter.core.engine.shutil.disk_usage",
        disk_usage,
    )
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        local_original_backup_dir=backup_base,
    )

    assert result["success"] is False
    assert "Insufficient local backup space" in result["error"]
    assert (mock_usb / "Contents" / "song.flac").is_file()
    assert not (mock_usb / "Contents" / "song.aiff").exists()


def test_local_archive_refuses_source_changed_after_copy(
    mock_usb: Path, tmp_path: Path, monkeypatch
):
    original_archive = LocalBackupSession.archive

    def archive_then_change(session, tasks, metadata_paths, progress_callback=None):
        task_list = list(tasks)
        original_archive(
            session,
            task_list,
            metadata_paths,
            progress_callback=progress_callback,
        )
        source = task_list[0].source_abs_path
        source.write_bytes(source.read_bytes() + b"changed after archive")

    monkeypatch.setattr(LocalBackupSession, "archive", archive_then_change)
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=True,
        backup=True,
        clean_dotfiles=False,
        local_original_backup_dir=tmp_path / "Local Backups",
    )

    assert result["success"] is False
    assert "changed after archiving" in result["error"]
    assert (mock_usb / "Contents" / "song.flac").is_file()
    assert not (mock_usb / "Contents" / "song.aiff").exists()


def test_restore_local_archive_rejects_manifest_path_traversal(
    mock_usb: Path, tmp_path: Path
):
    session = tmp_path / "malicious-session"
    session.mkdir()
    (session / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "usb_root": str(mock_usb),
                "originals": [
                    {
                        "usb_path": "../outside.flac",
                        "archive_path": "../outside.flac",
                    }
                ],
                "metadata": [],
            }
        ),
        encoding="utf-8",
    )

    restored, message = ConversionEngine().restore_local_backup(session, mock_usb)

    assert restored is False
    assert "Unsafe usb_path" in message


def _complete_onelibrary_bridge(mock_usb: Path):
    dlp = mock_usb / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    dlp.parent.mkdir(parents=True, exist_ok=True)
    dlp.write_bytes(b"OneLibrary before rebuild")
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb, allow_onelibrary_bridge=True),
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
        allow_onelibrary_bridge=True,
    )
    assert result["success"] is True
    return engine, dlp


def _mark_onelibrary_rebuilt(mock_usb: Path, dlp: Path):
    pdb = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    replacement = mock_usb / "Contents" / "song.aiff"
    rebuilt_ns = max(pdb.stat().st_mtime_ns, replacement.stat().st_mtime_ns) + 4_000_000_000
    dlp.write_bytes(b"OneLibrary rebuilt from Device Library")
    os.utime(dlp, ns=(rebuilt_ns, rebuilt_ns))


def test_cleanup_retained_originals_after_onelibrary_rebuild(mock_usb: Path):
    engine, dlp = _complete_onelibrary_bridge(mock_usb)
    source = mock_usb / "Contents" / "song.flac"
    unrelated = mock_usb / "Contents" / "unrelated.flac"
    unrelated.write_bytes(b"not referenced by the conversion backup")
    source_size = source.stat().st_size
    _mark_onelibrary_rebuilt(mock_usb, dlp)

    plan = engine.plan_retained_original_cleanup(mock_usb)

    assert plan.errors == []
    assert plan.onelibrary_rebuild_observed is True
    assert len(plan.candidates) == 1
    assert plan.candidates[0].original_usb_path == "/Contents/song.flac"
    assert plan.candidates[0].replacement_usb_path == "/Contents/song.aiff"
    assert plan.total_bytes == source_size

    result = engine.cleanup_retained_originals(plan)

    assert result["success"] is True
    assert result["removed"] == 1
    assert result["freed_bytes"] == source_size
    assert not source.exists()
    assert unrelated.is_file()
    assert (mock_usb / "Contents" / "song.aiff").is_file()


def test_cleanup_refuses_before_onelibrary_rebuild(mock_usb: Path):
    engine, _dlp = _complete_onelibrary_bridge(mock_usb)
    source = mock_usb / "Contents" / "song.flac"

    plan = engine.plan_retained_original_cleanup(mock_usb)
    result = engine.cleanup_retained_originals(plan)

    assert plan.onelibrary_rebuild_observed is False
    assert any("does not appear to have been rebuilt" in error for error in plan.errors)
    assert result["success"] is False
    assert source.is_file()


def test_cleanup_refuses_without_onelibrary(mock_usb: Path):
    engine = ConversionEngine()
    result = engine.execute(
        engine.scan(mock_usb),
        delete_original=False,
        backup=True,
        clean_dotfiles=False,
        threads=1,
    )
    source = mock_usb / "Contents" / "song.flac"

    assert result["success"] is True

    plan = engine.plan_retained_original_cleanup(mock_usb)
    cleanup_result = engine.cleanup_retained_originals(plan)

    assert any("OneLibrary was not found" in error for error in plan.errors)
    assert cleanup_result["success"] is False
    assert source.is_file()


def test_cleanup_refuses_stale_plan_when_original_changes(mock_usb: Path):
    engine, dlp = _complete_onelibrary_bridge(mock_usb)
    source = mock_usb / "Contents" / "song.flac"
    _mark_onelibrary_rebuilt(mock_usb, dlp)
    plan = engine.plan_retained_original_cleanup(mock_usb)
    source.write_bytes(source.read_bytes() + b"changed after cleanup plan")

    result = engine.cleanup_retained_originals(plan)

    assert result["success"] is False
    assert result["removed"] == 0
    assert source.is_file()


def test_cleanup_refuses_stale_plan_when_onelibrary_changes(mock_usb: Path):
    engine, dlp = _complete_onelibrary_bridge(mock_usb)
    source = mock_usb / "Contents" / "song.flac"
    _mark_onelibrary_rebuilt(mock_usb, dlp)
    plan = engine.plan_retained_original_cleanup(mock_usb)
    changed_ns = dlp.stat().st_mtime_ns + 4_000_000_000
    dlp.write_bytes(b"OneLibrary changed after cleanup preview")
    os.utime(dlp, ns=(changed_ns, changed_ns))

    result = engine.cleanup_retained_originals(plan)

    assert result["success"] is False
    assert result["removed"] == 0
    assert source.is_file()


def test_cleanup_refuses_missing_converted_replacement(mock_usb: Path):
    engine, dlp = _complete_onelibrary_bridge(mock_usb)
    source = mock_usb / "Contents" / "song.flac"
    _mark_onelibrary_rebuilt(mock_usb, dlp)
    (mock_usb / "Contents" / "song.aiff").unlink()

    plan = engine.plan_retained_original_cleanup(mock_usb)
    result = engine.cleanup_retained_originals(plan)

    assert any("converted replacement is missing" in error for error in plan.errors)
    assert result["success"] is False
    assert result["removed"] == 0
    assert source.is_file()


def test_cleanup_refuses_stale_device_sql_file_type(mock_usb: Path):
    engine, dlp = _complete_onelibrary_bridge(mock_usb)
    source = mock_usb / "Contents" / "song.flac"
    pdb_path = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    manager = PDBManager(pdb_path)
    track = manager.tracks[0]
    row_base = track.page_idx * manager.len_page + track.row_offset
    struct.pack_into("<H", manager.data, row_base + TRACK_FILE_TYPE_OFFSET, 0x05)
    manager.save(backup=False)
    _mark_onelibrary_rebuilt(mock_usb, dlp)

    plan = engine.plan_retained_original_cleanup(mock_usb)
    result = engine.cleanup_retained_originals(plan)

    assert any("file type does not match" in error for error in plan.errors)
    assert result["success"] is False
    assert result["removed"] == 0
    assert source.is_file()


def test_cleanup_refuses_stale_2ex_path(mock_usb: Path):
    engine, dlp = _complete_onelibrary_bridge(mock_usb)
    source = mock_usb / "Contents" / "song.flac"
    anlz_2ex = (
        mock_usb
        / "PIONEER"
        / "USBANLZ"
        / "P001"
        / "00000001"
        / "ANLZ0000.2EX"
    )
    anlz_2ex.write_bytes(anlz_2ex.with_suffix(".2EX.bak").read_bytes())
    _mark_onelibrary_rebuilt(mock_usb, dlp)

    plan = engine.plan_retained_original_cleanup(mock_usb)
    result = engine.cleanup_retained_originals(plan)

    assert any("ANLZ .2EX still references" in error for error in plan.errors)
    assert result["success"] is False
    assert result["removed"] == 0
    assert source.is_file()


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
    anlz_dir = mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    for suffix in (".DAT", ".EXT", ".2EX"):
        assert ANLZManager.read_path(anlz_dir / f"ANLZ0000{suffix}") == (
            "/Contents/song.flac"
        )


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


def test_engine_allows_stale_database_file_size(mock_usb: Path):
    pdb = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    data = bytearray(pdb.read_bytes())
    source = mock_usb / "Contents" / "song.flac"
    struct.pack_into("<I", data, 4096 + 0x28 + 0x10, source.stat().st_size + 4096)
    pdb.write_bytes(data)
    engine = ConversionEngine()

    result = engine.execute(
        engine.scan(mock_usb),
        backup=False,
        clean_dotfiles=False,
        threads=1,
    )

    assert result["success"] is True
    assert not source.exists()
    assert (mock_usb / "Contents" / "song.aiff").is_file()


def test_engine_rejects_source_changed_since_scan(mock_usb: Path):
    engine = ConversionEngine()
    summary = engine.scan(mock_usb)
    source = mock_usb / "Contents" / "song.flac"
    source.write_bytes(source.read_bytes() + b"changed after scan")

    result = engine.execute(summary, backup=False, clean_dotfiles=False, threads=1)

    assert result["success"] is False
    assert "changed since this scan" in result["preflight_errors"][0]
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
