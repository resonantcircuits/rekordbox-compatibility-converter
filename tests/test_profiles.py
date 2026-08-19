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


def test_standard_profile_detects_alac_inside_m4a():
    profile = get_profile(CompatibilityProfileType.STANDARD)
    track = TrackInfo(
        id=5,
        filename="track.m4a",
        sample_rate=44100,
        sample_depth=16,
        codec_name="alac",
    )

    result = profile.evaluate(track)
    assert result.is_compatible is False
    assert any("ALAC" in reason for reason in result.reasons)


def test_standard_profile_converts_32khz_flac_to_valid_pcm_rate():
    profile = get_profile(CompatibilityProfileType.STANDARD)
    track = TrackInfo(id=6, filename="track.flac", sample_rate=32000, sample_depth=16)

    result = profile.evaluate(track)
    assert result.suggested_sample_rate == 44100


def test_maximum_profile_rejects_48khz_aiff():
    profile = get_profile(CompatibilityProfileType.MAXIMUM)
    track = TrackInfo(id=7, filename="track.aiff", sample_rate=48000, sample_depth=16)

    assert profile.evaluate(track).is_compatible is False


def test_modern_profile_accepts_fla_extension():
    profile = get_profile(CompatibilityProfileType.MODERN)
    track = TrackInfo(id=8, filename="track.fla", sample_rate=96000, sample_depth=24)

    assert profile.evaluate(track).is_compatible is True


def test_standard_and_modern_profiles_accept_aac_in_mp4_container():
    track = TrackInfo(
        id=9,
        filename="track.mp4",
        codec_name="aac",
        sample_rate=44100,
        sample_depth=16,
    )

    assert get_profile(CompatibilityProfileType.STANDARD).evaluate(track).is_compatible is True
    assert get_profile(CompatibilityProfileType.MODERN).evaluate(track).is_compatible is True


def test_profiles_reject_alac_in_mp4_container():
    track = TrackInfo(
        id=10,
        filename="track.mp4",
        codec_name="alac",
        sample_rate=44100,
        sample_depth=16,
    )

    result = get_profile(CompatibilityProfileType.MODERN).evaluate(track)

    assert result.is_compatible is False
    assert any("only for AAC" in reason for reason in result.reasons)


def test_profiles_require_codec_inspection_for_mpeg4_containers():
    track = TrackInfo(id=11, filename="track.m4a", sample_rate=44100, sample_depth=16)

    result = get_profile(CompatibilityProfileType.MODERN).evaluate(track)

    assert result.is_compatible is False
    assert any("codec inspection" in reason for reason in result.reasons)


def test_profile_rejects_codec_that_does_not_match_extension():
    track = TrackInfo(
        id=12,
        filename="mislabeled.wav",
        codec_name="mp3",
        sample_rate=44100,
        sample_depth=16,
    )

    result = get_profile(CompatibilityProfileType.STANDARD).evaluate(track)

    assert result.is_compatible is False
    assert any("does not match" in reason for reason in result.reasons)
