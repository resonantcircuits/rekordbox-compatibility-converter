"""Tests for compatibility profiles."""

from rekordbox_compatibility_converter.core.models import (
    CompatibilityProfileType,
    TargetFormat,
    TrackInfo,
)
from rekordbox_compatibility_converter.core.profiles import get_profile


def test_standard_profile_flac():
    profile = get_profile(CompatibilityProfileType.STANDARD)
    flac_track = TrackInfo(
        id=1,
        filename="beat.flac",
        file_path="/Contents/Artist/Album/beat.flac",
        sample_rate=44100,
        sample_depth=16,
    )
    result = profile.evaluate(flac_track)
    assert result.is_compatible is False
    assert any("FLAC" in r for r in result.reasons)
    assert result.suggested_target_format == TargetFormat.AIFF
    assert result.suggested_sample_rate == 44100
    assert result.suggested_sample_depth == 16


def test_standard_profile_aiff_compatible():
    profile = get_profile(CompatibilityProfileType.STANDARD)
    aiff_track = TrackInfo(
        id=2,
        filename="beat.aiff",
        file_path="/Contents/Artist/Album/beat.aiff",
        sample_rate=44100,
        sample_depth=24,
    )
    result = profile.evaluate(aiff_track)
    assert result.is_compatible is True
    assert len(result.reasons) == 0


def test_standard_profile_high_samplerate():
    profile = get_profile(CompatibilityProfileType.STANDARD)
    wav_96k = TrackInfo(
        id=3,
        filename="track.wav",
        file_path="/Contents/Artist/Album/track.wav",
        sample_rate=96000,
        sample_depth=24,
    )
    result = profile.evaluate(wav_96k)
    assert result.is_compatible is False
    assert any("Sample rate" in r for r in result.reasons)
    assert result.suggested_sample_rate == 48000


def test_maximum_profile_strictly_16bit():
    profile = get_profile(CompatibilityProfileType.MAXIMUM)
    aiff_24bit = TrackInfo(
        id=4,
        filename="track.aiff",
        file_path="/Contents/Artist/Album/track.aiff",
        sample_rate=44100,
        sample_depth=24,
    )
    result = profile.evaluate(aiff_24bit)
    assert result.is_compatible is False
    assert result.suggested_sample_depth == 16
    assert result.suggested_sample_rate == 44100
