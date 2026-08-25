"""End-to-end self-test used by frozen desktop build jobs."""

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from .core.audio_converter import AudioConverter
from .core.folder_engine import FolderConversionEngine
from .core.models import CompatibilityProfileType, TargetFormat
from .core.profiles import get_profile


def verify_ffmpeg_distribution_metadata(
    bundle_root: Path,
    runtime_version_output: str,
    runtime_license_output: str,
) -> None:
    """Verify that the frozen app ships accurate FFmpeg notices and metadata."""
    notices_path = bundle_root / "THIRD_PARTY_NOTICES.txt"
    source_offer_path = bundle_root / "SOURCE_OFFER.txt"
    metadata_dir = bundle_root / "licenses" / "ffmpeg"
    build_info_path = metadata_dir / "FFMPEG_BUILD_INFO.txt"

    if not notices_path.is_file():
        raise RuntimeError("Packaged app is missing THIRD_PARTY_NOTICES.txt")
    notices = notices_path.read_text(encoding="utf-8-sig", errors="replace")
    if "ffmpeg" not in notices.lower():
        raise RuntimeError("THIRD_PARTY_NOTICES.txt does not identify FFmpeg")

    if not build_info_path.is_file():
        raise RuntimeError(
            "Packaged app is missing licenses/ffmpeg/FFMPEG_BUILD_INFO.txt"
        )
    build_info = build_info_path.read_text(
        encoding="utf-8-sig",
        errors="replace",
    )
    recorded_version = next(
        (line.strip() for line in build_info.splitlines() if line.strip()),
        "",
    )
    runtime_version = next(
        (line.strip() for line in runtime_version_output.splitlines() if line.strip()),
        "",
    )
    if not recorded_version.lower().startswith("ffmpeg version "):
        raise RuntimeError("FFMPEG_BUILD_INFO.txt does not contain FFmpeg version output")
    if recorded_version != runtime_version:
        raise RuntimeError(
            "FFMPEG_BUILD_INFO.txt does not match the bundled FFmpeg executable"
        )

    configuration = build_info.lower()
    forbidden_options = ("--enable-gpl", "--enable-nonfree")
    if any(option in configuration for option in forbidden_options):
        raise RuntimeError("Bundled FFmpeg enables GPL or nonfree components")
    required_options = (
        "--disable-autodetect",
        "--disable-everything",
        "--disable-network",
        "--enable-libmp3lame",
    )
    if any(option not in configuration for option in required_options):
        raise RuntimeError("Bundled FFmpeg is not the approved minimal build")
    if "lesser general public license" not in runtime_license_output.lower():
        raise RuntimeError("Bundled FFmpeg does not report an LGPL licence")

    required_licences = (
        metadata_dir / "COPYING.LGPLv2.1",
        bundle_root / "licenses" / "lame" / "COPYING",
        bundle_root / "licenses" / "rekordbox" / "LICENSE",
        bundle_root / "licenses" / "python" / "LICENSE.txt",
        bundle_root / "licenses" / "tcl-tk" / "tcl" / "license.terms",
        bundle_root / "licenses" / "tcl-tk" / "tk" / "license.terms",
        bundle_root / "licenses" / "python-packages" / "customtkinter" / "LICENSE",
        bundle_root / "licenses" / "python-packages" / "darkdetect" / "LICENSE",
        bundle_root / "licenses" / "python-packages" / "packaging" / "LICENSE",
    )
    for licence_path in required_licences:
        if not licence_path.is_file():
            raise RuntimeError(f"Packaged app is missing licence: {licence_path.name}")
        licence_text = licence_path.read_text(
            encoding="utf-8-sig",
            errors="replace",
        ).lower()
        legal_markers = ("license", "copyright", "redistribution", "permission")
        if len(licence_text.strip()) < 40 or not any(
            marker in licence_text for marker in legal_markers
        ):
            raise RuntimeError(f"Packaged licence is invalid: {licence_path.name}")

    if not source_offer_path.is_file():
        raise RuntimeError("Packaged app is missing SOURCE_OFFER.txt")
    source_offer = source_offer_path.read_text(
        encoding="utf-8-sig",
        errors="replace",
    ).lower()
    if ".tar.gz" not in source_offer or "download location:" not in source_offer:
        raise RuntimeError("SOURCE_OFFER.txt does not identify the source archive")


def _write_silent_wav(path: Path) -> None:
    """Create a tiny deterministic PCM input without relying on FFmpeg."""
    sample_rate = 44100
    frame_count = sample_rate // 20
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\0\0\0\0" * frame_count)


def run_frozen_conversion_smoke_test() -> None:
    """Verify bundled FFmpeg can probe and convert without using system tools."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Packaged conversion smoke test requires a frozen application")

    bundle_root = Path(getattr(sys, "_MEIPASS", "")).resolve()
    converter = AudioConverter()
    tools_ok, tools_message = converter.check_tools()
    if not tools_ok:
        raise RuntimeError(tools_message)

    for tool_path in (converter.ffmpeg_bin, converter.ffprobe_bin):
        if Path(tool_path).resolve().parent != bundle_root:
            raise RuntimeError(f"Packaged self-test resolved an external tool: {tool_path}")

    version_result = subprocess.run(
        [converter.ffmpeg_bin, "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    license_result = subprocess.run(
        [converter.ffmpeg_bin, "-L"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    verify_ffmpeg_distribution_metadata(
        bundle_root,
        version_result.stdout,
        license_result.stdout,
    )

    with tempfile.TemporaryDirectory(prefix="rbconvert-package-smoke-") as temp_dir:
        input_path = Path(temp_dir) / "input.wav"
        _write_silent_wav(input_path)
        decoded_hash = converter.decoded_audio_sha256(input_path)
        if len(decoded_hash) != 64:
            raise RuntimeError("Bundled FFmpeg decoded-audio hashing failed")

        outputs = [
            (TargetFormat.AIFF, "output.aiff", "pcm_s16be"),
            (TargetFormat.WAV, "output.wav", "pcm_s16le"),
            (TargetFormat.MP3, "output.mp3", "mp3"),
        ]
        for target_format, filename, expected_codec in outputs:
            output_path = Path(temp_dir) / filename
            success, output_size, error = converter.convert(
                input_path,
                output_path,
                target_format=target_format,
                sample_rate=44100,
                sample_depth=16,
            )
            if not success:
                raise RuntimeError(error or f"Packaged {target_format.value} conversion failed")
            if output_size <= 0 or not output_path.is_file():
                raise RuntimeError(
                    f"Packaged {target_format.value} conversion produced no output"
                )

            probe = converter.probe(output_path)
            expected = {
                "codec_name": expected_codec,
                "sample_rate": 44100,
                "bits_per_sample": 16,
                "channels": 2,
            }
            for field, expected_value in expected.items():
                if probe.get(field) != expected_value:
                    raise RuntimeError(
                        f"Packaged {target_format.value} output has unexpected "
                        f"{field}: {probe.get(field)!r}"
                    )

        folder_source = Path(temp_dir) / "folder-source"
        folder_source.mkdir()
        folder_input = folder_source / "nested" / "input.wav"
        folder_input.parent.mkdir()
        _write_silent_wav(folder_input)
        folder_destination = Path(temp_dir) / "folder-destination"
        folder_engine = FolderConversionEngine(audio_converter=converter)
        folder_summary = folder_engine.scan(
            folder_source,
            folder_destination,
            get_profile(CompatibilityProfileType.STANDARD),
            target_format=TargetFormat.AIFF,
            normalize_all=True,
        )
        if folder_summary.issues or len(folder_summary.tasks) != 1:
            raise RuntimeError(
                "Packaged standalone folder scan failed: "
                + "; ".join(folder_summary.issues)
            )
        folder_result = folder_engine.execute(folder_summary, threads=1)
        folder_output = folder_destination / "nested" / "input.aiff"
        if not folder_result.get("success") or not folder_output.is_file():
            raise RuntimeError("Packaged standalone folder conversion failed")
