"""FFmpeg-based audio converter with dithering, metadata preservation, and multi-format support."""

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from .models import TargetFormat
from .subprocess_utils import run_external


class AudioConverter:
    """Manages audio probing, format conversion, and metadata preservation."""

    def __init__(self, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe"):
        self.ffmpeg_bin = self._resolve_tool(ffmpeg_bin)
        self.ffprobe_bin = self._resolve_tool(ffprobe_bin)

    @staticmethod
    def _resolve_tool(requested: str) -> str:
        """Find a PyInstaller-bundled tool before falling back to PATH."""
        if requested in {"ffmpeg", "ffprobe"} and getattr(sys, "frozen", False):
            bundle_root = getattr(sys, "_MEIPASS", None)
            if bundle_root:
                executable_name = requested + (".exe" if os.name == "nt" else "")
                bundled_tool = Path(bundle_root) / executable_name
                if bundled_tool.is_file():
                    return str(bundled_tool)
        return shutil.which(requested) or requested

    def check_tools(self) -> Tuple[bool, str]:
        """Verifies bundled or system ffmpeg and ffprobe executables are available."""
        if not shutil.which(self.ffmpeg_bin):
            return False, f"ffmpeg binary not found at '{self.ffmpeg_bin}'"
        if not shutil.which(self.ffprobe_bin):
            return False, f"ffprobe binary not found at '{self.ffprobe_bin}'"
        return True, "FFmpeg tools available"

    def probe(self, file_path: Path) -> Dict:
        """Extracts format, sample rate, bit depth, channels, and tags."""
        file_path = Path(file_path)
        if not file_path.exists():
            return {}

        cmd = [
            self.ffprobe_bin,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
        try:
            result = run_external(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            info = json.loads(result.stdout)
            audio_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})

            if not audio_stream:
                return {"probe_error": "No audio stream found", "size": file_path.stat().st_size}

            sample_rate = int(audio_stream.get("sample_rate") or 0)
            codec_name = audio_stream.get("codec_name", "")
            bits_per_sample = int(audio_stream.get("bits_per_raw_sample") or audio_stream.get("bits_per_sample") or 0)
            if bits_per_sample == 0 and codec_name in {"mp3", "aac"}:
                bits_per_sample = 16
            channels = int(audio_stream.get("channels", 2))
            if sample_rate <= 0 or channels <= 0:
                return {
                    "probe_error": "Audio stream has invalid sample-rate or channel metadata",
                    "size": file_path.stat().st_size,
                }
            bit_rate = int(
                info.get("format", {}).get("bit_rate")
                or audio_stream.get("bit_rate")
                or (sample_rate * channels * bits_per_sample)
            )

            return {
                "format_name": info.get("format", {}).get("format_name", ""),
                "codec_name": codec_name,
                "sample_rate": sample_rate,
                "bits_per_sample": bits_per_sample,
                "channels": channels,
                "bit_rate": bit_rate,
                "duration": float(info.get("format", {}).get("duration", 0)),
                "size": int(info.get("format", {}).get("size", file_path.stat().st_size)),
                "tags": info.get("format", {}).get("tags", {}),
                "has_artwork": any(
                    stream.get("codec_type") == "video"
                    and bool(stream.get("disposition", {}).get("attached_pic"))
                    for stream in info.get("streams", [])
                ),
            }
        except Exception as exc:
            return {
                "probe_error": str(exc),
                "size": file_path.stat().st_size if file_path.exists() else 0,
            }

    def decoded_audio_sha256(self, file_path: Path) -> str:
        """Hash decoded audio frames so equivalent files can be compared safely."""
        cmd = [
            self.ffmpeg_bin,
            "-v", "error",
            "-i", str(file_path),
            "-map", "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-f", "hash",
            "-hash", "sha256",
            "-",
        ]
        result = run_external(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        output = result.stdout.strip()
        if not output.startswith("SHA256="):
            raise ValueError(f"FFmpeg returned an invalid decoded-audio hash: {output}")
        return output.removeprefix("SHA256=").strip().lower()

    def convert(
        self,
        source_path: Path,
        target_path: Path,
        target_format: TargetFormat = TargetFormat.AIFF,
        sample_rate: int = 44100,
        sample_depth: int = 16,
        dither: bool = True,
    ) -> Tuple[bool, int, Optional[str]]:
        """Converts an audio file to the target format with metadata, dithering, and artwork preservation.

        Returns (success, new_filesize_bytes, error_message).
        """
        source_path = Path(source_path)
        target_path = Path(target_path)

        if not source_path.exists():
            return False, 0, f"Source file does not exist: {source_path}"
        if not source_path.is_file():
            return False, 0, f"Source path is not a regular file: {source_path}"
        if target_path.exists():
            return False, 0, f"Refusing to overwrite existing target: {target_path}"
        if sample_rate <= 0:
            return False, 0, f"Invalid target sample rate: {sample_rate}"
        if target_format in {TargetFormat.AIFF, TargetFormat.WAV} and sample_depth not in {16, 24}:
            return False, 0, f"Unsupported target PCM depth: {sample_depth}-bit"

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, 0, f"Could not create target directory: {exc}"
        tmp_target = target_path.with_name(
            f".{target_path.stem}.{uuid.uuid4().hex}.tmp{target_path.suffix}"
        )

        fmt_flag = target_format.value
        if target_format == TargetFormat.AIFF:
            fmt_flag = "aiff"
        elif target_format == TargetFormat.WAV:
            fmt_flag = "wav"
        elif target_format == TargetFormat.MP3:
            fmt_flag = "mp3"

        cmd = [
            self.ffmpeg_bin,
            "-y",
            # Without fatal decode errors, FFmpeg can accept a truncated source
            # and emit a shorter but superficially valid output.
            "-xerror",
            "-i", str(source_path),
            "-map", "0:a:0",
            "-ar", str(sample_rate),
            "-map_metadata", "0",
            "-f", fmt_flag,
        ]

        source_info = self.probe(source_path)
        source_rate = int(source_info.get("sample_rate") or 0)
        source_depth = int(source_info.get("bits_per_sample") or 0)
        source_channels = int(source_info.get("channels") or 0)

        # Club players consume mono or stereo program audio. Preserve mono and
        # stereo sources exactly, but downmix multichannel material to stereo.
        if source_channels > 2:
            cmd.extend(["-ac", "2"])

        # TPDF dithering is useful for bit-depth reduction or resampling, but
        # should not be forced onto a same-rate, same-depth lossless transfer.
        if dither and (
            (source_depth and sample_depth < source_depth)
            or (source_rate and sample_rate != source_rate)
        ):
            cmd.extend(["-dither_method", "triangular"])

        if target_format == TargetFormat.AIFF:
            codec = "pcm_s16be" if sample_depth == 16 else "pcm_s24be"
            cmd.extend(["-map", "0:v?", "-c:a", codec, "-c:v", "mjpeg", "-write_id3v2", "1"])
        elif target_format == TargetFormat.WAV:
            codec = "pcm_s16le" if sample_depth == 16 else "pcm_s24le"
            cmd.extend(["-c:a", codec])
        elif target_format == TargetFormat.MP3:
            cmd.extend([
                "-map", "0:v?", "-c:a", "libmp3lame", "-b:a", "320k",
                "-c:v", "mjpeg", "-id3v2_version", "3",
            ])
        else:
            codec = "pcm_s16be"
            cmd.extend(["-c:a", codec])

        cmd.append(str(tmp_target))

        try:
            res = run_external(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if res.returncode != 0:
                if tmp_target.exists():
                    tmp_target.unlink()
                return False, 0, f"FFmpeg error: {res.stderr.strip()}"

            if not tmp_target.exists() or tmp_target.stat().st_size == 0:
                tmp_target.unlink(missing_ok=True)
                return False, 0, "FFmpeg produced empty output file."

            output_info = self.probe(tmp_target)
            if output_info.get("probe_error"):
                tmp_target.unlink(missing_ok=True)
                return False, 0, f"Converted output failed validation: {output_info['probe_error']}"
            if int(output_info.get("sample_rate") or 0) != sample_rate:
                tmp_target.unlink(missing_ok=True)
                return False, 0, "Converted output has an unexpected sample rate."
            expected_channels = 2 if source_channels > 2 else source_channels
            if expected_channels and int(output_info.get("channels") or 0) != expected_channels:
                tmp_target.unlink(missing_ok=True)
                return False, 0, "Converted output has an unexpected channel count."
            if target_format in {TargetFormat.AIFF, TargetFormat.WAV}:
                expected_codec = {
                    (TargetFormat.AIFF, 16): "pcm_s16be",
                    (TargetFormat.AIFF, 24): "pcm_s24be",
                    (TargetFormat.WAV, 16): "pcm_s16le",
                    (TargetFormat.WAV, 24): "pcm_s24le",
                }.get((target_format, sample_depth))
                if not expected_codec or output_info.get("codec_name") != expected_codec:
                    tmp_target.unlink(missing_ok=True)
                    return False, 0, "Converted output has an unexpected PCM encoding."
                if int(output_info.get("bits_per_sample") or 0) != sample_depth:
                    tmp_target.unlink(missing_ok=True)
                    return False, 0, "Converted output has an unexpected sample depth."
            elif target_format == TargetFormat.MP3 and output_info.get("codec_name") != "mp3":
                tmp_target.unlink(missing_ok=True)
                return False, 0, "Converted output is not MP3 audio."

            source_duration = float(source_info.get("duration") or 0)
            output_duration = float(output_info.get("duration") or 0)
            if source_duration > 0 and output_duration > 0:
                duration_tolerance = max(0.25, source_duration * 0.001)
                if abs(source_duration - output_duration) > duration_tolerance:
                    tmp_target.unlink(missing_ok=True)
                    return (
                        False,
                        0,
                        "Converted output duration differs from the source "
                        f"({source_duration:.3f}s source, {output_duration:.3f}s output).",
                    )

            with open(tmp_target, "r+b") as converted_file:
                os.fsync(converted_file.fileno())
            if target_path.exists():
                tmp_target.unlink(missing_ok=True)
                return False, 0, f"Target appeared during conversion; refusing to overwrite: {target_path}"
            os.replace(tmp_target, target_path)
            if os.name == "posix":
                try:
                    directory_fd = os.open(str(target_path.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            new_size = target_path.stat().st_size
            return True, new_size, None

        except Exception as e:
            if tmp_target.exists():
                tmp_target.unlink()
            return False, 0, str(e)
