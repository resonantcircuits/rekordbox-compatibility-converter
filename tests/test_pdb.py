"""Tests for PDB manager and DeviceSQL manipulation."""

import struct
from pathlib import Path
import pytest
from rekordbox_format_checker.core.models import TrackInfo
from rekordbox_format_checker.core.pdb_manager import PDBManager


def create_minimal_pdb(tmp_path: Path, file_size: int = 50000000) -> Path:
    """Creates a valid, minimal DeviceSQL export.pdb for testing."""
    len_page = 4096
    num_tables = 1
    seq_db = 1

    # Page 0: File Header
    page0 = bytearray(len_page)
    struct.pack_into("<IIII", page0, 4, len_page, num_tables, 2, seq_db)
    # Table 0 (tracks): first_page=1, last_page=1
    struct.pack_into("<IIII", page0, 0x1C, 0, 0, 1, 1)

    # Page 1: Table 0 Data Page
    page1 = bytearray(len_page)
    # Page header
    struct.pack_into("<IIIII", page1, 0, 0, 1, 0, 0, 1)  # gap, page_idx, type=0, next_page=0, seq=1
    # Page flags & rows: 1 row offset, 1 row, flags=0x24 (data page)
    raw18 = (0x24 << 24) | (1 << 13) | 1
    struct.pack_into("<I", page1, 0x18, raw18)
    struct.pack_into("<HHHH", page1, 0x1C, 3500, 500, 0, 0)

    # Heap: row at offset 0 (which starts at 0x28)
    row_base = 0x28
    sample_rate = 44100
    bitrate = 1411200
    track_id = 101
    sample_depth = 16
    duration = 240

    struct.pack_into("<I", page1, row_base + 0x08, sample_rate)
    struct.pack_into("<I", page1, row_base + 0x10, file_size)
    struct.pack_into("<I", page1, row_base + 0x30, bitrate)
    struct.pack_into("<I", page1, row_base + 0x48, track_id)
    struct.pack_into("<H", page1, row_base + 0x52, sample_depth)
    struct.pack_into("<H", page1, row_base + 0x54, duration)

    # Encode strings in heap after row fixed struct (0x88 bytes)
    # Title at row_base + 0x88
    # Filename at row_base + 0xA0
    # Filepath at row_base + 0xC0
    ofs_title = 0x88
    ofs_fn = 0xA0
    ofs_fp = 0xC0

    # ofs_strings: 21 * u2 at row_base + 0x5e
    struct.pack_into("<H", page1, row_base + 0x5E + (17 * 2), ofs_title)
    struct.pack_into("<H", page1, row_base + 0x5E + (19 * 2), ofs_fn)
    struct.pack_into("<H", page1, row_base + 0x5E + (20 * 2), ofs_fp)

    # Encode "Test Song" (len 9) -> header = (10 << 1) | 1 = 21
    title_bytes = b"Test Song"
    page1[row_base + ofs_title] = (len(title_bytes) + 1) * 2 + 1
    page1[row_base + ofs_title + 1 : row_base + ofs_title + 1 + len(title_bytes)] = title_bytes

    # Encode "song.flac" (len 9)
    fn_bytes = b"song.flac"
    page1[row_base + ofs_fn] = (len(fn_bytes) + 1) * 2 + 1
    page1[row_base + ofs_fn + 1 : row_base + ofs_fn + 1 + len(fn_bytes)] = fn_bytes

    # Encode "/Contents/song.flac" (len 19)
    fp_bytes = b"/Contents/song.flac"
    page1[row_base + ofs_fp] = (len(fp_bytes) + 1) * 2 + 1
    page1[row_base + ofs_fp + 1 : row_base + ofs_fp + 1 + len(fp_bytes)] = fp_bytes

    # Row Index Group at end of page
    # Base for group 0 is len_page = 4096
    # row_present_flags at base - 4: 0x0001 (1st row present)
    struct.pack_into("<H", page1, len_page - 4, 1)
    # Row offset 0 is at base - 6: 0x0000 (offset from 0x28)
    struct.pack_into("<H", page1, len_page - 6, 0)

    pdb_file = tmp_path / "export.pdb"
    with open(pdb_file, "wb") as f:
        f.write(page0 + page1)

    return pdb_file


def test_pdb_parse_and_update(tmp_path: Path):
    pdb_path = create_minimal_pdb(tmp_path)
    mgr = PDBManager(pdb_path)

    assert len(mgr.tracks) == 1
    track = mgr.tracks[0]
    assert track.id == 101
    assert track.title == "Test Song"
    assert track.filename == "song.flac"
    assert track.file_path == "/Contents/song.flac"
    assert track.sample_rate == 44100
    assert track.sample_depth == 16
    assert track.file_size == 50000000

    # Update track to song.aiff
    mgr.update_track(
        track=track,
        new_filename="song.aiff",
        new_filepath="/Contents/song.aiff",
        new_filesize=60000000,
        new_sample_rate=44100,
        new_sample_depth=16,
        new_bitrate=1411200,
    )
    mgr.save(backup=False)

    # Reload and verify
    mgr2 = PDBManager(pdb_path)
    assert len(mgr2.tracks) == 1
    t2 = mgr2.tracks[0]
    assert t2.filename == "song.aiff"
    assert t2.file_path == "/Contents/song.aiff"
    assert t2.file_size == 60000000
