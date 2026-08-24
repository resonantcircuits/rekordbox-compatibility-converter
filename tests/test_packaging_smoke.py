"""Tests for frozen-application packaging validation."""

from pathlib import Path

import pytest

from rekordbox_compatibility_converter.packaging_smoke import (
    verify_ffmpeg_distribution_metadata,
)


VERSION_OUTPUT = """ffmpeg version 8.1.1 Copyright (c) FFmpeg developers
configuration: --disable-autodetect --disable-everything --disable-network --enable-libmp3lame
"""
LICENSE_OUTPUT = "FFmpeg is licensed under the GNU Lesser General Public License."


def _write_valid_metadata(bundle_root: Path) -> None:
    (bundle_root / "THIRD_PARTY_NOTICES.txt").write_text(
        "This application bundles FFmpeg and ffprobe.",
        encoding="utf-8",
    )
    metadata_dir = bundle_root / "licenses" / "ffmpeg"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "FFMPEG_BUILD_INFO.txt").write_text(
        VERSION_OUTPUT,
        encoding="utf-8",
    )
    (metadata_dir / "COPYING.LGPLv2.1").write_text(
        "GNU LESSER GENERAL PUBLIC LICENSE Version 2.1",
        encoding="utf-8",
    )
    lame_dir = bundle_root / "licenses" / "lame"
    lame_dir.mkdir()
    (lame_dir / "COPYING").write_text(
        "GNU LESSER GENERAL PUBLIC LICENSE Version 2",
        encoding="utf-8",
    )
    app_dir = bundle_root / "licenses" / "rekordbox"
    app_dir.mkdir()
    permissive_licence = (
        "MIT License\nCopyright example\nPermission is hereby granted to use this software."
    )
    (app_dir / "LICENSE").write_text(permissive_licence, encoding="utf-8")
    python_dir = bundle_root / "licenses" / "python"
    python_dir.mkdir()
    (python_dir / "LICENSE.txt").write_text(
        "Python Software Foundation License\nCopyright Python Software Foundation.",
        encoding="utf-8",
    )
    tcl_tk_dir = bundle_root / "licenses" / "tcl-tk"
    tcl_tk_dir.mkdir()
    (tcl_tk_dir / "license.terms").write_text(
        "Copyright Tcl/Tk contributors. Permission is granted to use this software.",
        encoding="utf-8",
    )
    for package_name in ("customtkinter", "darkdetect", "packaging"):
        package_dir = bundle_root / "licenses" / "python-packages" / package_name
        package_dir.mkdir(parents=True)
        (package_dir / "LICENSE").write_text(
            permissive_licence,
            encoding="utf-8",
        )
    (bundle_root / "SOURCE_OFFER.txt").write_text(
        "Archive: RekordboxFormatChecker-FFmpeg-Sources.tar.gz\n"
        "Download location: https://example.invalid/releases\n",
        encoding="utf-8",
    )


def test_ffmpeg_distribution_metadata_accepts_matching_bundle(tmp_path):
    _write_valid_metadata(tmp_path)

    verify_ffmpeg_distribution_metadata(tmp_path, VERSION_OUTPUT, LICENSE_OUTPUT)


@pytest.mark.parametrize(
    ("relative_path", "expected_error"),
    [
        ("THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.txt"),
        (
            "licenses/ffmpeg/FFMPEG_BUILD_INFO.txt",
            "FFMPEG_BUILD_INFO.txt",
        ),
        ("licenses/ffmpeg/COPYING.LGPLv2.1", "licence"),
        ("licenses/lame/COPYING", "licence"),
        ("SOURCE_OFFER.txt", "SOURCE_OFFER.txt"),
    ],
)
def test_ffmpeg_distribution_metadata_rejects_missing_files(
    tmp_path,
    relative_path,
    expected_error,
):
    _write_valid_metadata(tmp_path)
    (tmp_path / relative_path).unlink()

    with pytest.raises(RuntimeError, match=expected_error):
        verify_ffmpeg_distribution_metadata(tmp_path, VERSION_OUTPUT, LICENSE_OUTPUT)


def test_ffmpeg_distribution_metadata_rejects_wrong_version(tmp_path):
    _write_valid_metadata(tmp_path)

    with pytest.raises(RuntimeError, match="does not match"):
        verify_ffmpeg_distribution_metadata(
            tmp_path,
            "ffmpeg version 7.0 Copyright (c) FFmpeg developers\n",
            LICENSE_OUTPUT,
        )


@pytest.mark.parametrize("forbidden_option", ["--enable-gpl", "--enable-nonfree"])
def test_ffmpeg_distribution_metadata_rejects_forbidden_builds(
    tmp_path,
    forbidden_option,
):
    _write_valid_metadata(tmp_path)
    build_info = tmp_path / "licenses" / "ffmpeg" / "FFMPEG_BUILD_INFO.txt"
    invalid_output = VERSION_OUTPUT + f"configuration: {forbidden_option}\n"
    build_info.write_text(invalid_output, encoding="utf-8")

    with pytest.raises(RuntimeError, match="GPL or nonfree"):
        verify_ffmpeg_distribution_metadata(
            tmp_path,
            invalid_output,
            LICENSE_OUTPUT,
        )
