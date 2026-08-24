"""Data models for Rekordbox format checker and converter."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional


class TargetFormat(str, Enum):
    AIFF = "aiff"
    WAV = "wav"
    MP3 = "mp3"


class RekordboxFileType(IntEnum):
    """Audio format codes stored in DeviceSQL track rows."""

    UNKNOWN = 0x00
    MP3 = 0x01
    M4A = 0x04
    FLAC = 0x05
    WAV = 0x0B
    AIFF = 0x0C


REKORDBOX_FILE_TYPE_BY_EXTENSION: Dict[str, RekordboxFileType] = {
    "mp3": RekordboxFileType.MP3,
    "m4a": RekordboxFileType.M4A,
    "mp4": RekordboxFileType.M4A,
    "flac": RekordboxFileType.FLAC,
    "fla": RekordboxFileType.FLAC,
    "wav": RekordboxFileType.WAV,
    "wave": RekordboxFileType.WAV,
    "aif": RekordboxFileType.AIFF,
    "aiff": RekordboxFileType.AIFF,
}

REKORDBOX_FILE_TYPE_BY_TARGET: Dict[TargetFormat, RekordboxFileType] = {
    TargetFormat.AIFF: RekordboxFileType.AIFF,
    TargetFormat.WAV: RekordboxFileType.WAV,
    TargetFormat.MP3: RekordboxFileType.MP3,
}


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
    file_type: int = RekordboxFileType.UNKNOWN
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
    anlz_2ex_path: Optional[Path] = None
    source_size_at_scan: Optional[int] = None
    source_mtime_ns_at_scan: Optional[int] = None
    existing_target_track_id: Optional[int] = None
    reuse_existing_target: bool = False
    adopt_existing_target: bool = False
    status: str = "pending"
    new_file_size: int = 0
    output_probe: Dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class OriginalCleanupCandidate:
    track_id: int
    track_title: str
    original_usb_path: str
    original_abs_path: Path
    replacement_usb_path: str
    replacement_abs_path: Path
    original_size: int
    original_mtime_ns: int
    replacement_size: int
    replacement_mtime_ns: int


@dataclass
class AnalysisPathRepair:
    """A metadata-only repair for stale ANLZ audio-path references."""

    track: TrackInfo
    old_audio_path: str
    new_audio_path: str
    sidecar_paths: List[Path] = field(default_factory=list)


@dataclass
class OriginalCleanupPlan:
    usb_root: Path
    candidates: List[OriginalCleanupCandidate] = field(default_factory=list)
    total_bytes: int = 0
    has_onelibrary: bool = False
    onelibrary_path: Optional[Path] = None
    onelibrary_rebuild_observed: bool = False
    onelibrary_sha256: str = ""
    onelibrary_mtime_ns: int = 0
    current_pdb_sha256: str = ""
    backup_pdb_sha256: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class ScanSummary:
    usb_root: Path
    total_tracks: int = 0
    compatible_tracks: int = 0
    incompatible_tracks: int = 0
    format_counts: dict = field(default_factory=dict)
    tasks: List[ConversionTask] = field(default_factory=list)
    analysis_repairs: List[AnalysisPathRepair] = field(default_factory=list)
    has_export_pdb: bool = False
    has_export_ext_pdb: bool = False
    has_dlp: bool = False
    onelibrary_bridge_mode: bool = False
    free_space_bytes: int = 0
    estimated_extra_bytes: int = 0
    required_space_bytes: int = 0
    required_space_with_local_backup_bytes: int = 0
    local_backup_required_space_bytes: int = 0
    unsupported_reason: Optional[str] = None
    pdb_sha256: str = ""
