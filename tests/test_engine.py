"""End-to-end integration tests for ConversionEngine with multi-threading, dotfile cleanup, and restore."""

import subprocess
from pathlib import Path
import pytest
from rekordbox_format_checker.core.engine import ConversionEngine
from rekordbox_format_checker.core.models import CompatibilityProfileType, TargetFormat
from rekordbox_format_checker.core.profiles import get_profile
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
    create_minimal_pdb(pioneer_dir, file_size=flac_path.stat().st_size)

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

    # Verify restore backup works
    success, msg = engine.restore_backup(mock_usb)
    assert success is True
    assert (mock_usb / "PIONEER" / "rekordbox" / "export.pdb").exists()
