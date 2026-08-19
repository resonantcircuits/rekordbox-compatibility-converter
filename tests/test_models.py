"""Tests for data models."""

from pathlib import Path
from rekordbox_compatibility_converter.core.models import (
    TrackInfo,
    TargetFormat,
    CompatibilityProfileType,
    ScanSummary,
    ConversionTask,
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


def test_scan_summary():
    summary = ScanSummary(usb_root=Path("/fake/usb"))
    assert summary.total_tracks == 0
    assert summary.compatible_tracks == 0
    assert summary.incompatible_tracks == 0
    assert summary.has_export_pdb is False
