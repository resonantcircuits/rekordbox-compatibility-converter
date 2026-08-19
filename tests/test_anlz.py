"""Tests for ANLZ file parser and patcher."""

import struct
from pathlib import Path
from rekordbox_compatibility_converter.core.anlz_manager import ANLZManager


def create_minimal_anlz(tmp_path: Path, audio_path: str) -> Path:
    """Creates a minimal valid PMAI ANLZ file with PPTH and PQTZ tags."""
    # PMAI Header: magic (4s), len_header (u4), len_file (u4)
    # len_header = 28
    encoded_path = audio_path.encode("utf-16be") + b"\x00\x00"
    len_path = len(encoded_path)
    tag_len = 12 + 4 + len_path

    # PPTH Tag: fourcc (4s), hdr_len (u4), tag_len (u4), len_path (u4), path (bytes)
    ppth_tag = struct.pack(">4sIII", b"PPTH", 12, tag_len, len_path) + encoded_path

    # PQTZ Tag (dummy beatgrid): 24 bytes
    pqtz_tag = struct.pack(">4sIII", b"PQTZ", 12, 24, 0) + (b"\x00" * 8)

    total_len = 28 + len(ppth_tag) + len(pqtz_tag)
    header = struct.pack(">4sII", b"PMAI", 28, total_len) + (b"\x00" * 16)

    anlz_file = tmp_path / "ANLZ0000.DAT"
    with open(anlz_file, "wb") as f:
        f.write(header + ppth_tag + pqtz_tag)

    return anlz_file


def test_anlz_read_and_update(tmp_path: Path):
    orig_path = "/Contents/Artist/Album/track.flac"
    anlz_file = create_minimal_anlz(tmp_path, orig_path)

    # Read path
    read_val = ANLZManager.read_path(anlz_file)
    assert read_val == orig_path

    # Update path
    new_path = "/Contents/Artist/Album/track.aiff"
    success = ANLZManager.update_path(anlz_file, new_path, backup=True)
    assert success is True

    # Verify updated path
    read_val2 = ANLZManager.read_path(anlz_file)
    assert read_val2 == new_path

    # Verify backup exists
    assert (tmp_path / "ANLZ0000.DAT.bak").exists()


def test_anlz_corrupt_zero_length_tag_does_not_hang(tmp_path: Path):
    """A malformed section with tag_len=0 must not send the parser into an infinite loop."""
    header = struct.pack(">4sII", b"PMAI", 28, 64) + (b"\x00" * 16)
    bad_tag = struct.pack(">4sIII", b"PQTZ", 12, 0, 0)
    anlz_file = tmp_path / "ANLZ0001.DAT"
    anlz_file.write_bytes(header + bad_tag + b"\x00" * 8)

    assert ANLZManager.read_path(anlz_file) is None
    assert ANLZManager.update_path(anlz_file, "/Contents/x.aiff") is False
