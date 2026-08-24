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
    tag_len = 16 + len_path

    # PPTH Tag: fourcc (4s), hdr_len (u4), tag_len (u4), len_path (u4), path (bytes)
    ppth_tag = (
        struct.pack(">4sIII", b"PPTH", 16, tag_len, len_path)
        + encoded_path
    )

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


def test_anlz_even_length_path_does_not_gain_trailing_null(tmp_path: Path):
    anlz_file = create_minimal_anlz(tmp_path, "/xx.flac")
    original_size = anlz_file.stat().st_size

    assert ANLZManager.update_path(anlz_file, "/xx.aiff") is True
    assert ANLZManager.read_path(anlz_file) == "/xx.aiff"
    assert anlz_file.stat().st_size == original_size

    data = anlz_file.read_bytes()
    ppth_len = struct.unpack_from(">I", data, 28 + 8)[0]
    assert ppth_len % 4 == 2
    assert data[28 + ppth_len : 32 + ppth_len] == b"PQTZ"


def test_anlz_path_resize_preserves_following_analysis_tags(tmp_path: Path):
    anlz_file = create_minimal_anlz(tmp_path, "/Contents/x.flac")
    before = anlz_file.read_bytes()
    old_ppth_len = struct.unpack_from(">I", before, 28 + 8)[0]
    analysis_tags = before[28 + old_ppth_len :]

    assert ANLZManager.update_path(
        anlz_file, "/Contents/Artist/Album/a-much-longer-track-name.aiff"
    )

    after = anlz_file.read_bytes()
    new_ppth_len = struct.unpack_from(">I", after, 28 + 8)[0]
    assert after[28 + new_ppth_len :] == analysis_tags
    assert struct.unpack_from(">I", after, 8)[0] == len(after)


def test_anlz_2ex_rewrite_preserves_three_band_waveforms(tmp_path: Path):
    audio_path = "/Contents/three-band.flac"
    encoded_path = audio_path.encode("utf-16be") + b"\x00\x00"
    ppth = struct.pack(
        ">4sIII", b"PPTH", 16, 16 + len(encoded_path), len(encoded_path)
    ) + encoded_path
    waveform_tags = (
        struct.pack(">4sII", b"PWV7", 12, 19)
        + b"detail!"
        + struct.pack(">4sII", b"PWV6", 12, 20)
        + b"preview!"
        + struct.pack(">4sII", b"PWVC", 12, 17)
        + b"vocal"
    )
    total_len = 28 + len(ppth) + len(waveform_tags)
    anlz_file = tmp_path / "ANLZ0000.2EX"
    anlz_file.write_bytes(
        struct.pack(">4sII", b"PMAI", 28, total_len)
        + (b"\x00" * 16)
        + ppth
        + waveform_tags
    )

    assert ANLZManager.update_path(anlz_file, "/Contents/three-band.aiff")

    rewritten = anlz_file.read_bytes()
    ppth_len = struct.unpack_from(">I", rewritten, 28 + 8)[0]
    assert ANLZManager.read_path(anlz_file) == "/Contents/three-band.aiff"
    assert rewritten[28 + ppth_len :] == waveform_tags


def test_anlz_truncated_ppth_is_rejected_without_exception(tmp_path: Path):
    header = struct.pack(">4sII", b"PMAI", 28, 40) + (b"\x00" * 16)
    truncated = struct.pack(">4sII", b"PPTH", 16, 12)
    anlz_file = tmp_path / "ANLZ0002.DAT"
    anlz_file.write_bytes(header + truncated)

    assert ANLZManager.read_path(anlz_file) is None
    assert ANLZManager.update_path(anlz_file, "/Contents/x.aiff") is False


def test_anlz_rejects_incorrect_file_length_header(tmp_path: Path):
    anlz_file = create_minimal_anlz(tmp_path, "/Contents/song.flac")
    data = bytearray(anlz_file.read_bytes())
    struct.pack_into(">I", data, 8, len(data) + 4)
    anlz_file.write_bytes(data)

    assert ANLZManager.read_path(anlz_file) is None
    assert ANLZManager.update_path(anlz_file, "/Contents/song.aiff") is False
