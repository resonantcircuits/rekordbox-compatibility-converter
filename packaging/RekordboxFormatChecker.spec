"""Cross-platform PyInstaller definition for the desktop application."""

import os
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


PROJECT_ROOT = Path(SPEC).resolve().parent.parent
APP_NAME = "RekordboxFormatChecker"
APP_DISPLAY_NAME = "Rekordbox Format Checker"
APP_IDENTIFIER = "com.resonantcircuits.rekordbox-format-checker"


def project_version():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in pyproject.splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("Could not determine project version from pyproject.toml")


def required_tool(name):
    configured = os.environ.get(f"RBCONVERT_{name.upper()}_PATH")
    resolved = configured or shutil.which(name)
    if not resolved or not Path(resolved).is_file():
        raise RuntimeError(
            f"{name} was not found; set RBCONVERT_{name.upper()}_PATH before building"
        )
    return str(Path(resolved).resolve())


def required_file(environment_name):
    configured = os.environ.get(environment_name)
    if not configured or not Path(configured).is_file():
        raise RuntimeError(f"Set {environment_name} to an existing file before building")
    return str(Path(configured).resolve())


def required_directory(environment_name):
    configured = os.environ.get(environment_name)
    if not configured or not Path(configured).is_dir():
        raise RuntimeError(f"Set {environment_name} to an existing directory before building")
    return str(Path(configured).resolve())


def generate_ffmpeg_build_info(ffmpeg_path):
    """Record details from the exact FFmpeg executable being bundled."""
    result = subprocess.run(
        [ffmpeg_path, "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    if not result.stdout.lower().startswith("ffmpeg version "):
        raise RuntimeError(f"Could not read FFmpeg build information from {ffmpeg_path}")
    metadata_dir = Path(workpath) / "ffmpeg-metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    build_info_path = metadata_dir / "FFMPEG_BUILD_INFO.txt"
    build_info_path.write_text(result.stdout, encoding="utf-8")
    return str(build_info_path)


def generate_source_offer():
    """Describe the corresponding-source artifact shipped beside this build."""
    archive_name = os.environ.get(
        "RBCONVERT_SOURCE_ARCHIVE_NAME",
        "RekordboxFormatChecker-FFmpeg-Sources.tar.gz",
    )
    download_url = os.environ.get(
        "RBCONVERT_SOURCE_DOWNLOAD_URL",
        "https://github.com/resonantcircuits/rekordbox-compatibility-converter/releases",
    )
    metadata_dir = Path(workpath) / "ffmpeg-metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    offer_path = metadata_dir / "SOURCE_OFFER.txt"
    offer_path.write_text(
        "Corresponding source for bundled FFmpeg and LAME\n\n"
        f"Archive distributed alongside this application: {archive_name}\n"
        f"Download location: {download_url}\n\n"
        "The archive contains the exact upstream source archives, pinned checksums, "
        "licences, and build script used for this application. Access is provided "
        "at no additional charge.\n",
        encoding="utf-8",
    )
    return str(offer_path)


def distribution_licences(distribution_name):
    """Collect licence files from a bundled Python distribution."""
    package = distribution(distribution_name)
    collected = []
    for relative_path in package.files or ():
        filename = Path(str(relative_path)).name.lower()
        if filename.startswith(("license", "copying")):
            source = Path(package.locate_file(relative_path)).resolve()
            if source.is_file():
                collected.append(
                    (str(source), f"licenses/python-packages/{distribution_name}")
                )
    if not collected:
        raise RuntimeError(f"Could not find licence files for {distribution_name}")
    return collected


def python_runtime_licence():
    candidates = (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "Lib" / "LICENSE.txt",
        Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise RuntimeError("Could not find the Python runtime licence")


VERSION = project_version()
VERSION_PARTS = tuple(int(part) for part in VERSION.split("."))
FILE_VERSION = (VERSION_PARTS + (0, 0, 0, 0))[:4]

ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")
ffmpeg_path = required_tool("ffmpeg")
runtime_binaries = ctk_binaries + [
    (ffmpeg_path, "."),
    (required_tool("ffprobe"), "."),
]
runtime_data = ctk_datas + [
    (str(PROJECT_ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(PROJECT_ROOT / "LICENSE"), "licenses/rekordbox"),
    (
        str(PROJECT_ROOT / "packaging" / "licenses" / "tcl" / "license.terms"),
        "licenses/tcl-tk/tcl",
    ),
    (
        str(PROJECT_ROOT / "packaging" / "licenses" / "tk" / "license.terms"),
        "licenses/tcl-tk/tk",
    ),
    (required_directory("RBCONVERT_FFMPEG_LEGAL_DIR"), "licenses"),
    (generate_ffmpeg_build_info(ffmpeg_path), "licenses/ffmpeg"),
    (generate_source_offer(), "."),
]
runtime_data += distribution_licences("customtkinter")
runtime_data += distribution_licences("darkdetect")
runtime_data += distribution_licences("packaging")
runtime_data.append((python_runtime_licence(), "licenses/python"))

a = Analysis(
    [str(PROJECT_ROOT / "rekordbox_compatibility_converter" / "gui" / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=runtime_binaries,
    datas=runtime_data,
    hiddenimports=ctk_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name=APP_NAME,
    )
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier=APP_IDENTIFIER,
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": APP_DISPLAY_NAME,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
        },
    )
elif sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=FILE_VERSION,
            prodvers=FILE_VERSION,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "Resonant Circuits"),
                            StringStruct("FileDescription", APP_DISPLAY_NAME),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", APP_NAME),
                            StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                            StringStruct("ProductName", APP_DISPLAY_NAME),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        version=version_info,
    )
else:
    raise RuntimeError(f"Desktop packaging is not configured for {sys.platform}")
