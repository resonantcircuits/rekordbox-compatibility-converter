"""Tests for ExportValidator."""

import struct
from pathlib import Path
from rekordbox_compatibility_converter.core.pdb_manager import (
    PDBManager,
    TRACK_FILE_TYPE_OFFSET,
)
from rekordbox_compatibility_converter.core.validator import ExportValidator
from tests.test_anlz import create_minimal_anlz
from tests.test_engine import mock_usb
from tests.test_pdb import create_minimal_pdb


def test_validator_on_mock_usb(mock_usb: Path):
    validator = ExportValidator()
    report = validator.validate(mock_usb)

    assert report.total_tracks_checked == 1
    assert report.passed_tracks == 1
    assert report.failed_tracks == 0
    assert len(report.issues) == 0


def test_validator_rejects_file_type_that_disagrees_with_extension(mock_usb: Path):
    pdb_path = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    manager = PDBManager(pdb_path)
    track = manager.tracks[0]
    row_base = track.page_idx * manager.len_page + track.row_offset
    struct.pack_into("<H", manager.data, row_base + TRACK_FILE_TYPE_OFFSET, 0x0C)
    manager.save(backup=False)

    report = ExportValidator().validate(mock_usb)

    assert report.failed_tracks == 1
    assert any("file type does not match .flac" in issue.message for issue in report.issues)


def test_validator_rejects_bogus_audio_and_invalid_anlz(tmp_path: Path):
    usb = tmp_path / "USB"
    rekordbox = usb / "PIONEER" / "rekordbox"
    anlz_dir = usb / "PIONEER" / "USBANLZ"
    contents = usb / "Contents"
    rekordbox.mkdir(parents=True)
    anlz_dir.mkdir(parents=True)
    contents.mkdir()

    audio = contents / "song.flac"
    audio.write_bytes(b"not audio")
    create_minimal_pdb(
        rekordbox,
        file_size=audio.stat().st_size,
        analyze_path="/PIONEER/USBANLZ/ANLZ0000.DAT",
    )
    (anlz_dir / "ANLZ0000.DAT").write_bytes(
        struct.pack(">4sII", b"PMAI", 28, 52)
        + (b"\x00" * 16)
        + struct.pack(">4sII", b"PQTZ", 12, 24)
        + (b"\x00" * 12)
    )
    (anlz_dir / "ANLZ0000.EXT").write_bytes(b"corrupt")

    report = ExportValidator().validate(usb)

    assert report.passed_tracks == 0
    assert report.failed_tracks == 1
    assert any("cannot be decoded" in issue.message.lower() for issue in report.issues)
    assert any("no valid PPTH" in issue.message for issue in report.issues)


def test_validator_rejects_anlz_path_outside_usb(tmp_path: Path):
    usb = tmp_path / "USB"
    rekordbox = usb / "PIONEER" / "rekordbox"
    contents = usb / "Contents"
    rekordbox.mkdir(parents=True)
    contents.mkdir()
    audio = contents / "song.flac"
    audio.write_bytes(b"not audio")
    create_minimal_pdb(
        rekordbox,
        file_size=audio.stat().st_size,
        analyze_path="/../outside.DAT",
    )
    (tmp_path / "outside.DAT").write_bytes(b"must not be parsed")

    report = ExportValidator().validate(usb)

    assert any("Unsafe ANLZ path" in issue.message for issue in report.issues)


def test_validator_rejects_stale_2ex_path(mock_usb: Path, tmp_path: Path):
    anlz_dir = mock_usb / "PIONEER" / "USBANLZ" / "P001" / "00000001"
    stale = create_minimal_anlz(tmp_path, "/Contents/stale.flac").read_bytes()
    (anlz_dir / "ANLZ0000.2EX").write_bytes(stale)

    report = ExportValidator().validate(mock_usb)

    assert report.failed_tracks == 1
    assert any(".2EX PPTH mismatch" in issue.message for issue in report.issues)


def test_validator_flags_bits_per_second_written_into_device_sql(mock_usb: Path):
    pdb_path = mock_usb / "PIONEER" / "rekordbox" / "export.pdb"
    manager = PDBManager(pdb_path)
    track = manager.tracks[0]
    row_base = track.page_idx * manager.len_page + track.row_offset
    struct.pack_into("<I", manager.data, row_base + 0x30, 1411200)
    manager.save(backup=False)
    validator = ExportValidator()
    validator.audio_converter.probe = lambda _path: {
        "sample_rate": 44100,
        "bits_per_sample": 16,
        "channels": 2,
        "codec_name": "pcm_s16be",
        "bit_rate": 1411200,
    }

    report = validator.validate(mock_usb)

    assert report.failed_tracks == 1
    assert any(
        "DB says 1411200 kbps" in issue.message
        and "about 1411 kbps" in issue.message
        for issue in report.issues
    )
