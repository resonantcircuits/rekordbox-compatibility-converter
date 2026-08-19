"""Comprehensive integrity validator for Rekordbox USB exports."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .anlz_manager import ANLZManager
from .audio_converter import AudioConverter
from .pdb_manager import PDBManager


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

    def validate(self, usb_root: Path) -> ValidationReport:
        usb_root = Path(usb_root).resolve()
        pdb_path = usb_root / "PIONEER" / "rekordbox" / "export.pdb"

        report = ValidationReport(usb_root=usb_root)

        if not pdb_path.exists():
            report.issues.append(
                ValidationIssue(
                    track_id=0,
                    track_title="",
                    severity="ERROR",
                    message="PIONEER/rekordbox/export.pdb not found.",
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

        for track in pdb_mgr.tracks:
            ext = track.extension.lower()
            report.format_counts[ext] = report.format_counts.get(ext, 0) + 1

            track_has_issue = False
            rel_audio_path = track.file_path.lstrip("/")
            audio_abs = usb_root / rel_audio_path

            # 1. Verify audio file existence
            if not audio_abs.exists():
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
                    report.issues.append(
                        ValidationIssue(
                            track_id=track.id,
                            track_title=track.title or track.filename,
                            severity="WARNING",
                            message=f"File size mismatch: DB says {track.file_size} bytes, disk has {actual_size} bytes",
                        )
                    )

            # 3. Verify ANLZ files and PPTH tags
            if track.analyze_path:
                anlz_rel = track.analyze_path.lstrip("/")
                anlz_dat_abs = usb_root / anlz_rel
                if not anlz_dat_abs.exists():
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
                    if anlz_path_str and anlz_path_str != track.file_path:
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

            if track_has_issue:
                report.failed_tracks += 1
            else:
                report.passed_tracks += 1

        return report
