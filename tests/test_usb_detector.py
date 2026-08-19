"""USB export detection tests."""

from rekordbox_compatibility_converter.core.usb_detector import USBDetector


def test_contents_directory_alone_is_not_a_rekordbox_export(tmp_path):
    (tmp_path / "Contents").mkdir()

    assert USBDetector.is_rekordbox_export_dir(tmp_path) is False


def test_device_library_plus_export_is_detected(tmp_path):
    database = tmp_path / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"database")

    assert USBDetector.is_rekordbox_export_dir(tmp_path) is True
