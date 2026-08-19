"""CLI behavior and process-status tests."""

from click.testing import CliRunner

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
    assert "Device Library Plus" in result.output
