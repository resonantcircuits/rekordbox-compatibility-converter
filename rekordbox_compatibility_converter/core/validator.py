"""Comprehensive integrity validator for Rekordbox USB exports."""

from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from .anlz_manager import ANLZManager
from .audio_converter import AudioConverter
from .dlp_manager import ONELIBRARY_PRESENT_MESSAGE
from .models import REKORDBOX_FILE_TYPE_BY_EXTENSION
from .pdb_manager import PDBManager, device_sql_bitrate_kbps
from .profiles import HardwareProfile


@dataclass
class ValidationIssue:
    track_id: int
    track_title: str
    severity: str  # "ERROR" or "WARNING"
    message: str


@dataclass
class ValidationReport:
    usb_root: Path
    total_tracks_checked: int = 0
    passed_tracks: int = 0
    failed_tracks: int = 0
    issues: List[ValidationIssue] = field(default_factory=list)
    format_counts: Dict[str, int] = field(default_factory=dict)
    all_anlz_valid: bool = True
    all_audio_files_exist: bool = True


class ExportValidator:
    """Performs end-to-end verification on a Rekordbox USB export."""

    def __init__(self, audio_converter: Optional[AudioConverter] = None):
        self.audio_converter = audio_converter or AudioConverter()

    @staticmethod
    def _is_within(root: Path, candidate: Path) -> bool:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve(strict=False)
        return resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents

    def validate(
        self, usb_root: Path, profile: Optional[HardwareProfile] = None
    ) -> ValidationReport:
        usb_root = Path(usb_root).resolve()
        pdb_path = usb_root / "PIONEER" / "rekordbox" / "export.pdb"

        report = ValidationReport(usb_root=usb_root)

        if not pdb_path.is_file():
            report.issues.append(
                ValidationIssue(
                    track_id=0,
                    track_title="",
                    severity="ERROR",
                    message="PIONEER/rekordbox/export.pdb not found.",
                )
            )
            return report
        if not self._is_within(usb_root, pdb_path):
            report.issues.append(
                ValidationIssue(
                    0,
                    "",
                    "ERROR",
                    "export.pdb resolves outside the selected USB root.",
                )
            )
            return report

        try:
            pdb_mgr = PDBManager(pdb_path)
        except Exception as e:
            report.issues.append(
                ValidationIssue(
                    track_id=0,
                    track_title="",
                    severity="ERROR",
                    message=f"Failed to parse export.pdb: {e}",
                )
            )
            return report

        report.total_tracks_checked = len(pdb_mgr.tracks)
        if not pdb_mgr.tracks:
            report.issues.append(
                ValidationIssue(0, "", "WARNING", "export.pdb contains no parsed track rows.")
            )

        dlp_paths = [
            usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db",
            usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db",
        ]
        if any(path.is_file() for path in dlp_paths):
            report.issues.append(
                ValidationIssue(
                    0,
                    "",
                    "ERROR",
                    ONELIBRARY_PRESENT_MESSAGE,
                )
            )

        for track in pdb_mgr.tracks:
            ext = track.extension.lower()
            report.format_counts[ext] = report.format_counts.get(ext, 0) + 1

            track_has_issue = False
            rel_audio_path = track.file_path.lstrip("/")
            audio_abs = usb_root / rel_audio_path

            expected_file_type = REKORDBOX_FILE_TYPE_BY_EXTENSION.get(ext)
            if expected_file_type is not None and track.file_type != expected_file_type:
                track_has_issue = True
                report.issues.append(
                    ValidationIssue(
                        track.id,
                        track.title or track.filename,
                        "ERROR",
                        f"Device Library file type does not match .{ext}: expected "
                        f"0x{expected_file_type:02x}, found 0x{track.file_type:02x}.",
                    )
                )

            if PurePosixPath(track.file_path).name != track.filename:
                track_has_issue = True
                report.issues.append(
                    ValidationIssue(
                        track.id,
                        track.title or track.filename,
                        "ERROR",
                        f"Database filename '{track.filename}' does not match path '{track.file_path}'.",
                    )
                )

            # 1. Verify audio file existence
            if not self._is_within(usb_root, audio_abs):
                report.all_audio_files_exist = False
                track_has_issue = True
                report.issues.append(
                    ValidationIssue(
                        track.id,
                        track.title or track.filename,
                        "ERROR",
                        f"Unsafe audio path escapes the USB root: {track.file_path}",
                    )
                )
            elif not audio_abs.is_file():
                report.all_audio_files_exist = False
                track_has_issue = True
                report.issues.append(
                    ValidationIssue(
                        track_id=track.id,
                        track_title=track.title or track.filename,
                        severity="ERROR",
                        message=f"Audio file missing on disk: {track.file_path}",
                    )
                )
            else:
                # 2. Verify file size matches database
                actual_size = audio_abs.stat().st_size
                if actual_size != track.file_size:
                    track_has_issue = True
                    report.issues.append(
                        ValidationIssue(
                            track_id=track.id,
                            track_title=track.title or track.filename,
                            severity="WARNING",
                            message=f"File size mismatch: DB says {track.file_size} bytes, disk has {actual_size} bytes",
                        )
                    )

                probe = self.audio_converter.probe(audio_abs)
                if probe.get("probe_error"):
                    track_has_issue = True
                    report.issues.append(
                        ValidationIssue(
                            track.id,
                            track.title or track.filename,
                            "ERROR",
                            f"Audio file cannot be decoded: {probe['probe_error']}",
                        )
                    )
                else:
                    actual_rate = int(probe.get("sample_rate") or 0)
                    actual_depth = int(probe.get("bits_per_sample") or 0)
                    actual_channels = int(probe.get("channels") or 0)
                    if actual_rate and actual_rate != track.sample_rate:
                        track_has_issue = True
                        report.issues.append(
                            ValidationIssue(
                                track.id,
                                track.title or track.filename,
                                "WARNING",
                                f"Sample-rate mismatch: DB says {track.sample_rate} Hz, audio has {actual_rate} Hz",
                            )
                        )
                    if actual_depth and actual_depth != track.sample_depth:
                        track_has_issue = True
                        report.issues.append(
                            ValidationIssue(
                                track.id,
                                track.title or track.filename,
                                "WARNING",
                                f"Bit-depth mismatch: DB says {track.sample_depth}-bit, audio reports {actual_depth}-bit",
                            )
                        )
                    codec_name = str(probe.get("codec_name") or "")
                    bitrate_bits_per_second = 0
                    if (
                        codec_name.startswith("pcm_")
                        and actual_rate
                        and actual_depth
                        and actual_channels
                    ):
                        bitrate_bits_per_second = (
                            actual_rate * actual_depth * actual_channels
                        )
                    elif codec_name == "mp3":
                        bitrate_bits_per_second = int(probe.get("bit_rate") or 0)
                    if bitrate_bits_per_second and track.bitrate:
                        expected_bitrate = device_sql_bitrate_kbps(
                            bitrate_bits_per_second
                        )
                        tolerance = max(2, expected_bitrate // 50)
                        if abs(track.bitrate - expected_bitrate) > tolerance:
                            track_has_issue = True
                            report.issues.append(
                                ValidationIssue(
                                    track.id,
                                    track.title or track.filename,
                                    "WARNING",
                                    f"Bitrate mismatch: DB says {track.bitrate} kbps, "
                                    f"audio reports about {expected_bitrate} kbps",
                                )
                            )
                    if profile:
                        actual_track = replace(
                            track,
                            codec_name=codec_name,
                            channels=actual_channels or 2,
                            sample_rate=actual_rate or track.sample_rate,
                            sample_depth=actual_depth or track.sample_depth,
                        )
                        compatibility = profile.evaluate(actual_track)
                        if not compatibility.is_compatible:
                            track_has_issue = True
                            report.issues.append(
                                ValidationIssue(
                                    track.id,
                                    track.title or track.filename,
                                    "ERROR",
                                    "Incompatible with selected profile: " + "; ".join(compatibility.reasons),
                                )
                            )

            # 3. Verify ANLZ files and PPTH tags
            if track.analyze_path:
                anlz_rel = track.analyze_path.lstrip("/")
                anlz_dat_abs = usb_root / anlz_rel
                if not self._is_within(usb_root, anlz_dat_abs):
                    report.all_anlz_valid = False
                    track_has_issue = True
                    report.issues.append(
                        ValidationIssue(
                            track.id,
                            track.title or track.filename,
                            "ERROR",
                            f"Unsafe ANLZ path escapes the USB root: {track.analyze_path}",
                        )
                    )
                elif not anlz_dat_abs.is_file():
                    report.all_anlz_valid = False
                    track_has_issue = True
                    report.issues.append(
                        ValidationIssue(
                            track_id=track.id,
                            track_title=track.title or track.filename,
                            severity="WARNING",
                            message=f"ANLZ .DAT file missing: {track.analyze_path}",
                        )
                    )
                else:
                    # Check internal PPTH path in ANLZ
                    anlz_path_str = ANLZManager.read_path(anlz_dat_abs)
                    if anlz_path_str is None:
                        report.all_anlz_valid = False
                        track_has_issue = True
                        report.issues.append(
                            ValidationIssue(
                                track.id,
                                track.title or track.filename,
                                "ERROR",
                                f"ANLZ .DAT has no valid PPTH path: {track.analyze_path}",
                            )
                        )
                    elif anlz_path_str != track.file_path:
                        report.all_anlz_valid = False
                        track_has_issue = True
                        report.issues.append(
                            ValidationIssue(
                                track_id=track.id,
                                track_title=track.title or track.filename,
                                severity="ERROR",
                                message=f"ANLZ PPTH path mismatch: ANLZ has '{anlz_path_str}', DB has '{track.file_path}'",
                            )
                        )

                    for suffix in (".EXT", ".2EX"):
                        sidecar = anlz_dat_abs.with_suffix(suffix)
                        if sidecar.exists() and not self._is_within(usb_root, sidecar):
                            report.all_anlz_valid = False
                            track_has_issue = True
                            report.issues.append(
                                ValidationIssue(
                                    track.id,
                                    track.title or track.filename,
                                    "ERROR",
                                    f"Unsafe ANLZ {suffix} path escapes the USB root: {sidecar}",
                                )
                            )
                        elif sidecar.is_file():
                            sidecar_path = ANLZManager.read_path(sidecar)
                            if sidecar_path is None:
                                report.all_anlz_valid = False
                                track_has_issue = True
                                report.issues.append(
                                    ValidationIssue(
                                        track.id,
                                        track.title or track.filename,
                                        "ERROR",
                                        f"ANLZ {suffix} has no valid PPTH path: "
                                        f"{sidecar.relative_to(usb_root)}",
                                    )
                                )
                            elif sidecar_path != track.file_path:
                                report.all_anlz_valid = False
                                track_has_issue = True
                                report.issues.append(
                                    ValidationIssue(
                                        track.id,
                                        track.title or track.filename,
                                        "ERROR",
                                        f"ANLZ {suffix} PPTH mismatch: ANLZ has "
                                        f"'{sidecar_path}', DB has '{track.file_path}'",
                                    )
                                )

            if track_has_issue:
                report.failed_tracks += 1
            else:
                report.passed_tracks += 1

        return report
