"""Hardware compatibility profiles for Pioneer CDJ/XDJ players."""

from typing import Dict, Optional, Protocol, Set
from .models import CompatibilityCheckResult, CompatibilityProfileType, TargetFormat


class AudioProperties(Protocol):
    extension: str
    codec_name: str
    sample_rate: int
    sample_depth: int


class HardwareProfile:
    """Represents rules and limits for a specific hardware generation profile."""

    def __init__(
        self,
        name: str,
        description: str,
        allowed_formats: Set[str],
        disallowed_formats: Set[str],
        max_sample_rate: int = 48000,
        allowed_sample_depths: Optional[Set[int]] = None,
        allowed_sample_rates: Optional[Dict[str, Set[int]]] = None,
        default_target_format: TargetFormat = TargetFormat.AIFF,
        target_sample_rate: Optional[int] = None,
        target_sample_depth: Optional[int] = None,
    ):
        self.name = name
        self.description = description
        self.allowed_formats = {f.lower() for f in allowed_formats}
        self.disallowed_formats = {f.lower() for f in disallowed_formats}
        self.max_sample_rate = max_sample_rate
        self.allowed_sample_depths = (
            set(allowed_sample_depths) if allowed_sample_depths is not None else {16, 24}
        )
        self.allowed_sample_rates = {
            key.lower(): set(rates) for key, rates in (allowed_sample_rates or {}).items()
        }
        self.default_target_format = default_target_format
        self.target_sample_rate = target_sample_rate
        self.target_sample_depth = target_sample_depth

    def evaluate(self, track: AudioProperties) -> CompatibilityCheckResult:
        ext = track.extension.lower()
        codec = track.codec_name.lower()
        effective_format = ext
        if ext in {"m4a", "mp4"}:
            if codec == "alac":
                effective_format = "alac"
            elif codec.startswith("aac"):
                effective_format = "aac"
        elif ext == "fla" and codec in {"", "flac"}:
            effective_format = "flac"

        reasons = []
        is_compatible = True

        expected_codecs = {
            "aiff": {"pcm_s16be", "pcm_s24be"},
            "aif": {"pcm_s16be", "pcm_s24be"},
            "wav": {"pcm_s16le", "pcm_s24le"},
            "mp3": {"mp3"},
            "aac": {"aac"},
            "flac": {"flac"},
            "fla": {"flac"},
        }
        if codec and ext in expected_codecs and codec not in expected_codecs[ext]:
            is_compatible = False
            reasons.append(
                f"File extension '.{ext.upper()}' does not match audio codec '{codec.upper()}'."
            )
        if ext in {"m4a", "mp4"} and not codec:
            is_compatible = False
            reasons.append(
                f"'.{ext.upper()}' requires codec inspection before compatibility can be confirmed."
            )
        elif ext == "mp4" and not codec.startswith("aac"):
            is_compatible = False
            reasons.append(
                f"File extension '.MP4' is supported only for AAC audio, not '{codec.upper()}'."
            )
        elif ext == "m4a" and codec != "alac" and not codec.startswith("aac"):
            is_compatible = False
            reasons.append(
                f"File extension '.M4A' does not match audio codec '{codec.upper()}'."
            )

        # Check format
        if ext in {"m4a", "mp4"} and ext not in self.allowed_formats:
            is_compatible = False
            reasons.append(f"Container '.{ext.upper()}' is not supported by {self.name} profile.")
        elif effective_format in self.disallowed_formats or ext in self.disallowed_formats:
            is_compatible = False
            label = codec.upper() if codec else ext.upper()
            reasons.append(f"Format '{label}' is not supported by {self.name} profile.")
        elif effective_format not in self.allowed_formats and ext not in self.allowed_formats:
            is_compatible = False
            reasons.append(f"Format '.{ext.upper()}' is unrecognized or unsupported.")

        # Check sample rate
        allowed_rates = self.allowed_sample_rates.get(
            effective_format, self.allowed_sample_rates.get(ext, set())
        )
        if allowed_rates and track.sample_rate not in allowed_rates:
            is_compatible = False
            reasons.append(
                f"Sample rate {track.sample_rate} Hz is not supported for {effective_format.upper() or 'this format'}; "
                f"allowed rates are {sorted(allowed_rates)} Hz."
            )
        elif not allowed_rates and track.sample_rate > self.max_sample_rate:
            is_compatible = False
            reasons.append(
                f"Sample rate {track.sample_rate} Hz exceeds maximum supported {self.max_sample_rate} Hz."
            )

        # Encoded AAC and MP3 do not have a hardware-facing PCM sample depth.
        # Rekordbox may store values such as 32 in this field even though the
        # decoder reports ordinary AAC/MP3 audio, so applying PCM depth limits
        # would create false incompatibility results.
        if (
            effective_format not in {"aac", "mp3"}
            and track.sample_depth not in self.allowed_sample_depths
        ):
            is_compatible = False
            reasons.append(
                f"Sample depth {track.sample_depth}-bit is not in supported depths {sorted(self.allowed_sample_depths)}."
            )

        # Determine target parameters
        suggested_format = self.default_target_format
        
        # Select a rate that is valid for the actual conversion target, not just
        # any numeric value below the hardware's maximum.
        if self.target_sample_rate:
            suggested_rate = self.target_sample_rate
        else:
            target_rates = self.allowed_sample_rates.get(
                self.default_target_format.value, {44100, 48000}
            )
            if track.sample_rate in target_rates:
                suggested_rate = track.sample_rate
            elif track.sample_rate % 44100 == 0 and 44100 in target_rates:
                suggested_rate = 44100
            elif track.sample_rate % 48000 == 0 and 48000 in target_rates:
                suggested_rate = 48000
            else:
                suggested_rate = min(target_rates, key=lambda rate: abs(rate - track.sample_rate))

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
        name="Standard Club (MP3/AAC/WAV/AIFF through 48 kHz)",
        description="Broad Rekordbox-player coverage. Converts FLAC/ALAC to AIFF and normalizes unsupported PCM rates to 44.1/48 kHz.",
        allowed_formats={"aiff", "aif", "wav", "mp3", "aac", "m4a", "mp4"},
        disallowed_formats={"flac", "alac", "ogg", "wma", "opus"},
        max_sample_rate=48000,
        allowed_sample_depths={16, 24},
        allowed_sample_rates={
            "aiff": {44100, 48000},
            "aif": {44100, 48000},
            "wav": {44100, 48000},
            "mp3": {16000, 22050, 24000, 32000, 44100, 48000},
            "aac": {16000, 22050, 24000, 32000, 44100, 48000},
            "m4a": {16000, 22050, 24000, 32000, 44100, 48000},
        },
        default_target_format=TargetFormat.AIFF,
    ),
    CompatibilityProfileType.MAXIMUM: HardwareProfile(
        name="Conservative 16-bit (AIFF/WAV/MP3 at 44.1 kHz)",
        description="A deliberately strict fallback preset that normalizes lossless audio to 16-bit 44.1 kHz and avoids AAC.",
        allowed_formats={"aiff", "aif", "wav", "mp3"},
        disallowed_formats={"flac", "alac", "m4a", "aac", "ogg", "wma", "opus"},
        max_sample_rate=48000,
        allowed_sample_depths={16},
        allowed_sample_rates={
            "aiff": {44100},
            "aif": {44100},
            "wav": {44100},
            "mp3": {44100},
        },
        default_target_format=TargetFormat.AIFF,
        target_sample_rate=44100,
        target_sample_depth=16,
    ),
    CompatibilityProfileType.MODERN: HardwareProfile(
        name="Modern Lossless (FLAC/ALAC through 48 kHz)",
        description="Modern players with FLAC/ALAC support. The shared 48 kHz ceiling keeps this profile safe across the listed CDJ/XDJ families.",
        allowed_formats={"flac", "fla", "alac", "aiff", "aif", "wav", "mp3", "aac", "m4a", "mp4"},
        disallowed_formats={"ogg", "wma", "opus"},
        max_sample_rate=48000,
        allowed_sample_depths={16, 24},
        allowed_sample_rates={
            "aiff": {44100, 48000},
            "aif": {44100, 48000},
            "wav": {44100, 48000},
            "flac": {44100, 48000},
            "alac": {44100, 48000},
            "mp3": {16000, 22050, 24000, 32000, 44100, 48000},
            "aac": {16000, 22050, 24000, 32000, 44100, 48000},
            "m4a": {16000, 22050, 24000, 32000, 44100, 48000},
        },
        default_target_format=TargetFormat.AIFF,
    ),
}


def get_profile(profile_type: CompatibilityProfileType = CompatibilityProfileType.STANDARD) -> HardwareProfile:
    return PROFILES.get(profile_type, PROFILES[CompatibilityProfileType.STANDARD])
