"""Tests for ExportValidator."""

from pathlib import Path
from rekordbox_compatibility_converter.core.validator import ExportValidator
from tests.test_engine import mock_usb


def test_validator_on_mock_usb(mock_usb: Path):
    validator = ExportValidator()
    report = validator.validate(mock_usb)

    assert report.total_tracks_checked == 1
    assert report.passed_tracks == 1
    assert report.failed_tracks == 0
    assert len(report.issues) == 0
