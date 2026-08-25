"""Tests for standalone audio-folder conversion."""

import hashlib
import subprocess
from pathlib import Path

from rekordbox_compatibility_converter.core.folder_engine import (
    FolderConversionEngine,
)
from rekordbox_compatibility_converter.core.models import (
    CompatibilityProfileType,
    TargetFormat,
)
from rekordbox_compatibility_converter.core.profiles import get_profile


def _make_audio(path: Path, codec_args=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.2",
        "-ar",
        "44100",
    ]
    command.extend(codec_args or [])
    command.append(str(path))
    subprocess.run(command, check=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_folder_engine_builds_complete_compatible_collection(tmp_path: Path):
    source = tmp_path / "Source"
    destination = tmp_path / "Compatible"
    flac = source / "Lossless" / "convert.flac"
    mp3 = source / "Ready" / "keep.mp3"
    _make_audio(flac)
    _make_audio(mp3, ["-c:a", "libmp3lame", "-b:a", "320k"])
    source_digests = {flac: _digest(flac), mp3: _digest(mp3)}
    engine = FolderConversionEngine()

    summary = engine.scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.STANDARD),
        target_format=TargetFormat.AIFF,
    )

    assert summary.issues == []
    assert summary.total_files == 2
    assert summary.compatible_files == 1
    assert summary.conversion_files == 1
    assert summary.copy_files == 1
    assert {task.action for task in summary.tasks} == {"copy", "convert"}
    assert not destination.exists()

    result = engine.execute(summary, threads=2)

    assert result["success"] is True
    assert result["converted"] == 1
    assert result["copied"] == 1
    converted = destination / "Lossless" / "convert.aiff"
    copied = destination / "Ready" / "keep.mp3"
    assert converted.is_file()
    assert copied.is_file()
    assert _digest(copied) == source_digests[mp3]
    assert {_digest(path) for path in source_digests} == set(source_digests.values())
    assert all(_digest(path) == digest for path, digest in source_digests.items())


def test_folder_engine_normalize_all_converts_compatible_files(tmp_path: Path):
    source = tmp_path / "Source"
    destination = tmp_path / "Normalized"
    mp3 = source / "track.mp3"
    _make_audio(mp3, ["-c:a", "libmp3lame", "-b:a", "320k"])

    summary = FolderConversionEngine().scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.MAXIMUM),
        target_format=TargetFormat.WAV,
        normalize_all=True,
    )

    assert summary.compatible_files == 1
    assert summary.conversion_files == 1
    assert summary.copy_files == 0
    assert summary.tasks[0].target_path == destination / "track.wav"
    assert summary.tasks[0].target_sample_rate == 44100
    assert summary.tasks[0].target_sample_depth == 16


def test_folder_engine_can_emit_only_files_needing_conversion(tmp_path: Path):
    source = tmp_path / "Source"
    destination = tmp_path / "Converted Only"
    _make_audio(source / "ready.mp3", ["-c:a", "libmp3lame"])
    _make_audio(source / "convert.flac")

    summary = FolderConversionEngine().scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.STANDARD),
        copy_compatible=False,
    )

    assert len(summary.tasks) == 1
    assert summary.tasks[0].audio.filename == "convert.flac"
    assert summary.copy_files == 0


def test_folder_engine_excludes_nested_destination_from_rescan(tmp_path: Path):
    source = tmp_path / "Source"
    destination = source / "Compatible Output"
    _make_audio(source / "original.flac")
    _make_audio(destination / "old.mp3", ["-c:a", "libmp3lame"])

    summary = FolderConversionEngine().scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.STANDARD),
    )

    assert summary.total_files == 1
    assert summary.tasks[0].audio.filename == "original.flac"


def test_folder_engine_blocks_output_collisions_before_conversion(tmp_path: Path):
    source = tmp_path / "Source"
    destination = tmp_path / "Destination"
    _make_audio(source / "same.flac")
    _make_audio(source / "same.m4a", ["-c:a", "alac"])

    engine = FolderConversionEngine()
    summary = engine.scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.STANDARD),
    )
    result = engine.execute(summary)

    assert any("Output collision" in issue for issue in summary.issues)
    assert result["success"] is False
    assert not destination.exists()


def test_folder_engine_refuses_existing_outputs_and_changed_sources(tmp_path: Path):
    source = tmp_path / "Source"
    destination = tmp_path / "Destination"
    flac = source / "track.flac"
    _make_audio(flac)
    destination.mkdir()
    (destination / "track.aiff").write_bytes(b"existing")
    engine = FolderConversionEngine()

    conflict = engine.scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.STANDARD),
    )
    assert any("Output already exists" in issue for issue in conflict.issues)

    (destination / "track.aiff").unlink()
    stale = engine.scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.STANDARD),
    )
    flac.write_bytes(flac.read_bytes() + b"changed")

    result = engine.execute(stale)

    assert result["success"] is False
    assert any("changed after scanning" in error for error in result["errors"])
    assert not (destination / "track.aiff").exists()


def test_folder_engine_requires_separate_source_and_destination(tmp_path: Path):
    source = tmp_path / "Source"
    source.mkdir()

    summary = FolderConversionEngine().scan(
        source,
        source,
        get_profile(CompatibilityProfileType.STANDARD),
    )

    assert any("must be different" in issue for issue in summary.issues)


def test_folder_engine_warns_when_wav_cannot_carry_embedded_artwork(tmp_path: Path):
    source = tmp_path / "Source"
    source.mkdir()
    audio = source / "artwork.flac"
    audio.write_bytes(b"fixture")

    class ArtworkProbe:
        def probe(self, _path):
            return {
                "sample_rate": 44100,
                "bits_per_sample": 16,
                "channels": 2,
                "codec_name": "flac",
                "duration": 1,
                "size": len(b"fixture"),
                "tags": {"title": "Artwork"},
                "has_artwork": True,
            }

    summary = FolderConversionEngine(audio_converter=ArtworkProbe()).scan(
        source,
        tmp_path / "Destination",
        get_profile(CompatibilityProfileType.STANDARD),
        target_format=TargetFormat.WAV,
    )

    assert summary.issues == []
    assert any("artwork cannot be represented" in warning for warning in summary.warnings)


def test_folder_engine_removes_output_if_source_changes_during_conversion(
    tmp_path: Path,
):
    source = tmp_path / "Source"
    source.mkdir()
    source_file = source / "track.flac"
    source_file.write_bytes(b"before")
    destination = tmp_path / "Destination"

    class MutatingConverter:
        def probe(self, path):
            return {
                "sample_rate": 44100,
                "bits_per_sample": 16,
                "channels": 2,
                "codec_name": "flac",
                "duration": 1,
                "size": path.stat().st_size,
            }

        def convert(self, source_path, target_path, **_kwargs):
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(b"output")
            source_path.write_bytes(b"changed during conversion")
            return True, target_path.stat().st_size, None

    engine = FolderConversionEngine(audio_converter=MutatingConverter())
    summary = engine.scan(
        source,
        destination,
        get_profile(CompatibilityProfileType.STANDARD),
    )

    result = engine.execute(summary, threads=1)

    assert result["success"] is False
    assert result["failed"] == 1
    assert "changed while output was being created" in result["errors"][0]
    assert not (destination / "track.aiff").exists()
