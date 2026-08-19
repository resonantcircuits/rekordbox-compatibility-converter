"""Hardware compatibility profiles for Pioneer CDJ/XDJ players."""

from typing import Optional, Set
from .models import CompatibilityCheckResult, CompatibilityProfileType, TargetFormat, TrackInfo


class HardwareProfile:
    """Represents rules and limits for a specific hardware generation profile."""

    def __init__(
        self,
        name: str,
        description: str,
        allowed_formats: Set[str],
        disallowed_formats: Set[str],
        max_sample_rate: int = 48000,
        allowed_sample_depths: Set[int] = None,
        default_target_format: TargetFormat = TargetFormat.AIFF,
        target_sample_rate: Optional[int] = None,
        target_sample_depth: Optional[int] = None,
    ):
        self.name = name
        self.description = description
        self.allowed_formats = {f.lower() for f in allowed_formats}
        self.disallowed_formats = {f.lower() for f in disallowed_formats}
        self.max_sample_rate = max_sample_rate
        self.allowed_sample_depths = allowed_sample_depths or {16, 24}
        self.default_target_format = default_target_format
        self.target_sample_rate = target_sample_rate
        self.target_sample_depth = target_sample_depth

    def evaluate(self, track: TrackInfo) -> CompatibilityCheckResult:
        ext = track.extension.lower()
        reasons = []
        is_compatible = True

        # Check format
        if ext in self.disallowed_formats:
            is_compatible = False
            reasons.append(f"Format '.{ext.upper()}' is not supported by {self.name} profile.")
        elif ext not in self.allowed_formats:
            is_compatible = False
            reasons.append(f"Format '.{ext.upper()}' is unrecognized or unsupported.")

        # Check sample rate
        if track.sample_rate > self.max_sample_rate:
            is_compatible = False
            reasons.append(
                f"Sample rate {track.sample_rate} Hz exceeds maximum supported {self.max_sample_rate} Hz."
            )

        # Check bit depth
        if track.sample_depth not in self.allowed_sample_depths:
            is_compatible = False
            reasons.append(
                f"Sample depth {track.sample_depth}-bit is not in supported depths {sorted(self.allowed_sample_depths)}."
            )

        # Determine target parameters
        suggested_format = self.default_target_format
        
        # Sample rate selection: keep 44.1/48k if valid, else clamp to 48000 or 44100
        if self.target_sample_rate:
            suggested_rate = self.target_sample_rate
        elif track.sample_rate in (44100, 48000):
            suggested_rate = track.sample_rate
        elif track.sample_rate in (88200, 176400):
            suggested_rate = 44100
        elif track.sample_rate >= 96000:
            suggested_rate = 48000
        else:
            suggested_rate = min(track.sample_rate, self.max_sample_rate)

        # Sample depth selection
        if self.target_sample_depth:
            suggested_depth = self.target_sample_depth
        elif track.sample_depth in self.allowed_sample_depths:
            suggested_depth = track.sample_depth
        else:
            suggested_depth = 24 if 24 in self.allowed_sample_depths else 16

        return CompatibilityCheckResult(
            is_compatible=is_compatible,
            reasons=reasons,
            suggested_target_format=suggested_format,
            suggested_sample_rate=suggested_rate,
            suggested_sample_depth=suggested_depth,
        )


PROFILES = {
    CompatibilityProfileType.STANDARD: HardwareProfile(
        name="Standard Club (CDJ-2000NXS, CDJ-900NXS, XDJ-1000/700/RX/RX2)",
        description="Standard club setup. Converts FLAC/ALAC to AIFF and downsizes >48kHz audio. Leaves MP3/WAV/AIFF intact.",
        allowed_formats={"aiff", "aif", "wav", "mp3", "aac", "m4a"},
        disallowed_formats={"flac", "alac", "ogg", "wma", "opus"},
        max_sample_rate=48000,
        allowed_sample_depths={16, 24},
        default_target_format=TargetFormat.AIFF,
    ),
    CompatibilityProfileType.MAXIMUM: HardwareProfile(
        name="Maximum Compatibility (CDJ-2000 orig, CDJ-850, CDJ-350, XDJ-AERO)",
        description="Legacy hardware setup. Strictly enforces 16-bit 44.1kHz AIFF/WAV/MP3 for vintage players.",
        allowed_formats={"aiff", "aif", "wav", "mp3"},
        disallowed_formats={"flac", "alac", "m4a", "aac", "ogg", "wma", "opus"},
        max_sample_rate=48000,
        allowed_sample_depths={16},
        default_target_format=TargetFormat.AIFF,
        target_sample_rate=44100,
        target_sample_depth=16,
    ),
    CompatibilityProfileType.MODERN: HardwareProfile(
        name="Modern Flagship (CDJ-3000, CDJ-2000NXS2, XDJ-XZ, XDJ-RX3, OPUS-QUAD)",
        description="Modern hardware supporting FLAC, ALAC, and high-res audio up to 24-bit 96kHz.",
        allowed_formats={"flac", "alac", "aiff", "aif", "wav", "mp3", "aac", "m4a"},
        disallowed_formats={"ogg", "wma", "opus"},
        max_sample_rate=96000,
        allowed_sample_depths={16, 24},
        default_target_format=TargetFormat.AIFF,
    ),
}


def get_profile(profile_type: CompatibilityProfileType = CompatibilityProfileType.STANDARD) -> HardwareProfile:
    return PROFILES.get(profile_type, PROFILES[CompatibilityProfileType.STANDARD])
