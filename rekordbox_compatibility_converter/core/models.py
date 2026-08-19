"""Data models for Rekordbox format checker and converter."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional


class TargetFormat(str, Enum):
    AIFF = "aiff"
    WAV = "wav"
    MP3 = "mp3"


class CompatibilityProfileType(str, Enum):
    STANDARD = "standard"  # CDJ-2000NXS, CDJ-900NXS, XDJ-1000, XDJ-700, XDJ-RX/RX2
    MAXIMUM = "maximum"    # CDJ-2000 orig, CDJ-900 orig, CDJ-850, CDJ-350, XDJ-AERO
    MODERN = "modern"      # CDJ-3000, CDJ-2000NXS2, XDJ-XZ, XDJ-RX3, OPUS-QUAD


@dataclass
class TrackInfo:
    id: int
    title: str = ""
    artist: str = ""
    album: str = ""
    filename: str = ""
    file_path: str = ""          # e.g. /Contents/Artist/Album/track.flac
    analyze_path: str = ""       # e.g. /PIONEER/USBANLZ/P001/00001234/ANLZ0000.DAT
    sample_rate: int = 44100
    sample_depth: int = 16
    bitrate: int = 1411200
    file_size: int = 0
    duration: int = 0
    codec_name: str = ""
    channels: int = 2
    page_idx: int = 0            # export.pdb page index
    row_offset: int = 0          # export.pdb offset inside page
    ofs_strings: tuple = field(default_factory=tuple)

    @property
    def extension(self) -> str:
        filename_suffix = PurePosixPath(self.filename).suffix
        path_suffix = PurePosixPath(self.file_path).suffix
        return (filename_suffix or path_suffix).lstrip(".").lower()


@dataclass
class CompatibilityCheckResult:
    is_compatible: bool
    reasons: List[str] = field(default_factory=list)
    suggested_target_format: TargetFormat = TargetFormat.AIFF
    suggested_sample_rate: int = 44100
    suggested_sample_depth: int = 16


@dataclass
class ConversionTask:
    track: TrackInfo
    source_abs_path: Path
    target_abs_path: Path
    target_usb_path: str
    target_filename: str
    target_format: TargetFormat
    target_sample_rate: int
    target_sample_depth: int
    anlz_dat_path: Optional[Path] = None
    anlz_ext_path: Optional[Path] = None
    status: str = "pending"
    new_file_size: int = 0
    output_probe: Dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ScanSummary:
    usb_root: Path
    total_tracks: int = 0
    compatible_tracks: int = 0
    incompatible_tracks: int = 0
    format_counts: dict = field(default_factory=dict)
    tasks: List[ConversionTask] = field(default_factory=list)
    has_export_pdb: bool = False
    has_export_ext_pdb: bool = False
    has_dlp: bool = False
    onelibrary_bridge_mode: bool = False
    free_space_bytes: int = 0
    estimated_extra_bytes: int = 0
    unsupported_reason: Optional[str] = None
    pdb_sha256: str = ""
