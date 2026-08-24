"""End-to-end self-test used by frozen desktop build jobs."""

import sys
import tempfile
import wave
from pathlib import Path

from .core.audio_converter import AudioConverter
from .core.models import TargetFormat


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

    with tempfile.TemporaryDirectory(prefix="rbconvert-package-smoke-") as temp_dir:
        input_path = Path(temp_dir) / "input.wav"
        _write_silent_wav(input_path)

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
