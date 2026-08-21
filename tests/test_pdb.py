"""Tests for PDB manager and DeviceSQL manipulation."""

import struct
from pathlib import Path
import pytest
from rekordbox_compatibility_converter.core.models import TrackInfo
from rekordbox_compatibility_converter.core.pdb_manager import (
    PDBManager,
    TRACK_FILE_TYPE_OFFSET,
)


def create_minimal_pdb(
    tmp_path: Path,
    file_size: int = 50000000,
    filename: str = "song.flac",
    filepath: str = "/Contents/song.flac",
    analyze_path: str = "",
    file_type: int = 0x05,
) -> Path:
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
    struct.pack_into("<H", page1, row_base + TRACK_FILE_TYPE_OFFSET, file_type)

    # Encode strings in heap after row fixed struct (0x88 bytes)
    # Title at row_base + 0x88
    # Filename at row_base + 0xA0
    # Filepath at row_base + 0xC0
    ofs_title = 0x88
    ofs_fn = 0xA0
    ofs_fp = 0x100
    ofs_analyze = 0x200

    # ofs_strings: 21 * u2 at row_base + 0x5e
    struct.pack_into("<H", page1, row_base + 0x5E + (17 * 2), ofs_title)
    struct.pack_into("<H", page1, row_base + 0x5E + (19 * 2), ofs_fn)
    struct.pack_into("<H", page1, row_base + 0x5E + (20 * 2), ofs_fp)
    if analyze_path:
        struct.pack_into("<H", page1, row_base + 0x5E + (14 * 2), ofs_analyze)

    # Encode "Test Song" (len 9) -> header = (10 << 1) | 1 = 21
    title_bytes = b"Test Song"
    page1[row_base + ofs_title] = (len(title_bytes) + 1) * 2 + 1
    page1[row_base + ofs_title + 1 : row_base + ofs_title + 1 + len(title_bytes)] = title_bytes

    def write_dsql_string(offset: int, value: str):
        try:
            encoded = value.encode("ascii")
            payload = bytes([(len(encoded) + 1) * 2 + 1]) + encoded
        except UnicodeEncodeError:
            encoded = value.encode("utf-16le")
            payload = struct.pack("<BHB", 0x90, len(encoded) + 4, 0x03) + encoded
        page1[row_base + offset : row_base + offset + len(payload)] = payload

    write_dsql_string(ofs_fn, filename)
    write_dsql_string(ofs_fp, filepath)
    if analyze_path:
        write_dsql_string(ofs_analyze, analyze_path)

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
    assert track.file_type == 0x05

    # Update track to song.aiff
    mgr.update_track(
        track=track,
        new_filename="song.aiff",
        new_filepath="/Contents/song.aiff",
        new_filesize=60000000,
        new_sample_rate=44100,
        new_sample_depth=16,
        new_bitrate=1411200,
        new_file_type=0x0C,
    )
    mgr.save(backup=False)

    # Reload and verify
    mgr2 = PDBManager(pdb_path)
    assert len(mgr2.tracks) == 1
    t2 = mgr2.tracks[0]
    assert t2.filename == "song.aiff"
    assert t2.file_path == "/Contents/song.aiff"
    assert t2.file_size == 60000000
    assert t2.file_type == 0x0C


def test_pdb_refuses_growing_string(tmp_path: Path):
    """A replacement longer than the original allocation must be rejected untouched,
    never written in place (it would overwrite adjacent heap data)."""
    pdb_path = create_minimal_pdb(tmp_path, filename="song.m4a", filepath="/Contents/song.m4a")
    mgr = PDBManager(pdb_path)
    track = mgr.tracks[0]
    data_before = bytes(mgr.data)

    assert mgr.can_fit_strings(track, "song.aiff", "/Contents/song.aiff") is False
    ok = mgr.update_track(
        track=track,
        new_filename="song.aiff",
        new_filepath="/Contents/song.aiff",
        new_filesize=60000000,
        new_sample_rate=44100,
        new_sample_depth=16,
        new_bitrate=1411200,
        new_file_type=0x0C,
    )
    assert ok is False
    assert bytes(mgr.data) == data_before

    # Same-length and shorter replacements still succeed
    assert mgr.can_fit_strings(track, "song.aiff"[:8], "/Contents/song.aiff"[:18]) is True
    assert (
        mgr.update_track(
            track,
            "song.mp3",
            "/Contents/song.mp3",
            1,
            44100,
            16,
            320000,
            0x01,
        )
        is True
    )
    mgr.save(backup=False)
    t2 = PDBManager(pdb_path).tracks[0]
    assert t2.filename == "song.mp3"
    assert t2.file_path == "/Contents/song.mp3"
    assert t2.file_type == 0x01


def test_pdb_rejects_string_allocation_outside_page(tmp_path: Path):
    pdb_path = create_minimal_pdb(tmp_path)
    data = bytearray(pdb_path.read_bytes())
    page_start = 4096
    row_base = page_start + 0x28
    malformed_offset = 4095 - 0x28
    struct.pack_into("<H", data, row_base + 0x5E + (19 * 2), malformed_offset)
    data[page_start + 4095] = 0x90
    pdb_path.write_bytes(data)

    manager = PDBManager(pdb_path)

    assert manager.tracks == []


def test_pdb_caps_malformed_row_group_count(tmp_path: Path):
    pdb_path = create_minimal_pdb(tmp_path)
    data = bytearray(pdb_path.read_bytes())
    struct.pack_into("<I", data, 4096 + 0x18, (0x24 << 24) | (1 << 13) | 0x1FFF)
    pdb_path.write_bytes(data)

    manager = PDBManager(pdb_path)

    assert len(manager.tracks) == 1


def test_pdb_rejects_table_directory_beyond_header_page(tmp_path: Path):
    pdb_path = create_minimal_pdb(tmp_path)
    data = bytearray(pdb_path.read_bytes())
    struct.pack_into("<I", data, 8, 1000)
    pdb_path.write_bytes(data)

    with pytest.raises(ValueError, match="table directory"):
        PDBManager(pdb_path)


def test_dsql_long_string_roundtrip(tmp_path: Path):
    """Long (>=127 byte) strings must encode with the header layout the reader expects."""
    pdb_path = create_minimal_pdb(tmp_path)
    mgr = PDBManager(pdb_path)

    long_path = "/Contents/" + "a" * 140 + ".aiff"
    encoded = mgr._encode_dsql_string(long_path)
    assert encoded[0] == 0x40

    buf = bytearray(1024)
    buf[0x88 : 0x88 + len(encoded)] = encoded
    text, pos, total = mgr._read_dsql_string(buf, 0, 0x88)
    assert text == long_path
    assert total == len(encoded)


def test_pdb_unicode_path_roundtrip(tmp_path: Path):
    pdb_path = create_minimal_pdb(
        tmp_path,
        filename="Beyoncé.flac",
        filepath="/Contents/Beyoncé.flac",
    )
    mgr = PDBManager(pdb_path)
    track = mgr.tracks[0]

    assert track.filename == "Beyoncé.flac"
    assert mgr.update_track(
        track,
        "Beyoncé.aiff",
        "/Contents/Beyoncé.aiff",
        1234,
        44100,
        16,
        1411200,
        0x0C,
    )
    mgr.save(backup=False)

    updated = PDBManager(pdb_path).tracks[0]
    assert updated.filename == "Beyoncé.aiff"
    assert updated.file_path == "/Contents/Beyoncé.aiff"
    assert updated.file_type == 0x0C
