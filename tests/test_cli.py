"""CLI behavior and process-status tests."""

from types import SimpleNamespace

from click.testing import CliRunner

from rekordbox_compatibility_converter.cli import main
from rekordbox_compatibility_converter.cli.main import cli


def test_scan_missing_database_returns_nonzero(tmp_path):
    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code != 0
    assert "export.pdb" in result.output


def test_scan_device_library_plus_returns_nonzero(tmp_path):
    database = tmp_path / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"unsupported")

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code != 0
    assert "OneLibrary" in result.output
    assert "No files were changed" in result.output
    assert "will not reliably fall back" in result.output


def test_cleanup_originals_cli_runs_verified_plan(tmp_path, monkeypatch):
    plan = SimpleNamespace(
        errors=[],
        candidates=[SimpleNamespace()],
        total_bytes=2 * 1024 ** 3,
        warnings=["Confirm the converted tracks in Rekordbox."],
    )

    class FakeEngine:
        def plan_retained_original_cleanup(self, usb_root):
            assert usb_root == tmp_path.resolve()
            return plan

        def cleanup_retained_originals(self, cleanup_plan):
            assert cleanup_plan is plan
            return {
                "success": True,
                "removed": 1,
                "failed": 0,
                "freed_bytes": plan.total_bytes,
            }

    monkeypatch.setattr(main, "ConversionEngine", FakeEngine)

    result = CliRunner().invoke(
        cli,
        ["cleanup-originals", str(tmp_path), "--yes"],
    )

    assert result.exit_code == 0
    assert "Removed 1 verified originals" in result.output
    assert "2.00 GiB" in result.output
