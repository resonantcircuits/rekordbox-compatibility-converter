"""Tests for data models."""

from pathlib import Path
from rekordbox_compatibility_converter.core.models import (
    CompatibilityProfileType,
    ConversionTask,
    REKORDBOX_FILE_TYPE_BY_EXTENSION,
    RekordboxFileType,
    ScanSummary,
    TargetFormat,
    TrackInfo,
)


def test_track_info_extension():
    t1 = TrackInfo(id=1, filename="Song.FLAC", file_path="/Contents/Artist/Album/Song.FLAC")
    assert t1.extension == "flac"

    t2 = TrackInfo(id=2, filename="Track", file_path="/Contents/Artist/Album/Track.aiff")
    assert t2.extension == "aiff"

    t3 = TrackInfo(id=3, filename="Track_no_ext", file_path="/Contents/Artist/Album/Track_no_ext")
    assert t3.extension == ""

    t4 = TrackInfo(id=4, filename="Track", file_path="/Contents/Artist.Name/Track")
    assert t4.extension == ""


def test_all_known_device_sql_file_types_are_mapped():
    assert REKORDBOX_FILE_TYPE_BY_EXTENSION == {
        "mp3": RekordboxFileType.MP3,
        "m4a": RekordboxFileType.M4A,
        "mp4": RekordboxFileType.M4A,
        "flac": RekordboxFileType.FLAC,
        "fla": RekordboxFileType.FLAC,
        "wav": RekordboxFileType.WAV,
        "wave": RekordboxFileType.WAV,
        "aif": RekordboxFileType.AIFF,
        "aiff": RekordboxFileType.AIFF,
    }
    assert {
        file_type for file_type in REKORDBOX_FILE_TYPE_BY_EXTENSION.values()
    } == {
        RekordboxFileType.MP3,
        RekordboxFileType.M4A,
        RekordboxFileType.FLAC,
        RekordboxFileType.WAV,
        RekordboxFileType.AIFF,
    }


def test_scan_summary():
    summary = ScanSummary(usb_root=Path("/fake/usb"))
    assert summary.total_tracks == 0
    assert summary.compatible_tracks == 0
    assert summary.incompatible_tracks == 0
    assert summary.has_export_pdb is False
    assert summary.onelibrary_bridge_mode is False
    assert summary.required_space_bytes == 0
    assert summary.required_space_with_local_backup_bytes == 0
    assert summary.local_backup_required_space_bytes == 0
