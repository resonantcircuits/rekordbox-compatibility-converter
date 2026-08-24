"""Tests for audio conversion with FFmpeg."""

import os
import subprocess
import sys
from pathlib import Path
import pytest
from rekordbox_compatibility_converter.core.audio_converter import AudioConverter
from rekordbox_compatibility_converter.core.models import TargetFormat


def test_audio_converter_prefers_bundled_tools_when_frozen(tmp_path, monkeypatch):
    executable_suffix = ".exe" if os.name == "nt" else ""
    ffmpeg = tmp_path / f"ffmpeg{executable_suffix}"
    ffprobe = tmp_path / f"ffprobe{executable_suffix}"
    ffmpeg.touch()
    ffprobe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    converter = AudioConverter()

    assert converter.ffmpeg_bin == str(ffmpeg)
    assert converter.ffprobe_bin == str(ffprobe)


@pytest.fixture
def sample_flac(tmp_path: Path) -> Path:
    """Generates a 0.5s test FLAC file using ffmpeg."""
    flac_file = tmp_path / "test.flac"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "sine=frequency=440:duration=0.5",
        "-ar", "44100",
        str(flac_file),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return flac_file


def test_audio_converter_probe(sample_flac: Path):
    converter = AudioConverter()
    info = converter.probe(sample_flac)
    assert info.get("sample_rate") == 44100
    assert info.get("channels") == 1 or info.get("channels") == 2
    assert info.get("size") > 0


def test_convert_flac_to_aiff(sample_flac: Path, tmp_path: Path):
    converter = AudioConverter()
    target_aiff = tmp_path / "test.aiff"

    success, new_size, err = converter.convert(
        source_path=sample_flac,
        target_path=target_aiff,
        target_format=TargetFormat.AIFF,
        sample_rate=44100,
        sample_depth=16,
    )

    assert success is True
    assert err is None
    assert target_aiff.exists()
    assert new_size > 0

    info = converter.probe(target_aiff)
    assert info.get("sample_rate") == 44100
    assert "aiff" in info.get("format_name", "").lower()

    assert converter.decoded_audio_sha256(target_aiff) == converter.decoded_audio_sha256(
        sample_flac
    )


def test_converted_output_fsync_uses_writable_descriptor(
    sample_flac: Path, tmp_path: Path, monkeypatch
):
    real_fsync = os.fsync

    def require_writable_descriptor(fd: int):
        os.write(fd, b"")
        real_fsync(fd)

    monkeypatch.setattr(
        "rekordbox_compatibility_converter.core.audio_converter.os.fsync",
        require_writable_descriptor,
    )

    success, _size, error = AudioConverter().convert(
        sample_flac,
        tmp_path / "synced.aiff",
        target_format=TargetFormat.AIFF,
    )

    assert success is True, error


def test_audio_converter_rejects_unsupported_pcm_depth(sample_flac: Path, tmp_path: Path):
    target = tmp_path / "invalid.aiff"

    success, size, error = AudioConverter().convert(
        sample_flac,
        target,
        target_format=TargetFormat.AIFF,
        sample_depth=32,
    )

    assert success is False
    assert size == 0
    assert "Unsupported target PCM depth" in error
    assert not target.exists()
