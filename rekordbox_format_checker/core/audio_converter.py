"""FFmpeg-based audio converter with dithering, metadata preservation, and multi-format support."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from .models import TargetFormat


class AudioConverter:
    """Manages audio probing, format conversion, and metadata preservation."""

    def __init__(self, ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe"):
        self.ffmpeg_bin = shutil.which(ffmpeg_bin) or ffmpeg_bin
        self.ffprobe_bin = shutil.which(ffprobe_bin) or ffprobe_bin

    def check_tools(self) -> Tuple[bool, str]:
        """Verifies ffmpeg and ffprobe are available in system PATH."""
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
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            info = json.loads(result.stdout)
            audio_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})

            sample_rate = int(audio_stream.get("sample_rate", 44100))
            bits_per_sample = int(audio_stream.get("bits_per_raw_sample") or audio_stream.get("bits_per_sample") or 16)
            channels = int(audio_stream.get("channels", 2))
            bit_rate = int(info.get("format", {}).get("bit_rate") or (sample_rate * channels * bits_per_sample))

            return {
                "format_name": info.get("format", {}).get("format_name", ""),
                "codec_name": audio_stream.get("codec_name", ""),
                "sample_rate": sample_rate,
                "bits_per_sample": bits_per_sample,
                "channels": channels,
                "bit_rate": bit_rate,
                "duration": float(info.get("format", {}).get("duration", 0)),
                "size": int(info.get("format", {}).get("size", file_path.stat().st_size)),
                "tags": info.get("format", {}).get("tags", {}),
            }
        except Exception:
            return {
                "sample_rate": 44100,
                "bits_per_sample": 16,
                "channels": 2,
                "bit_rate": 1411200,
                "size": file_path.stat().st_size if file_path.exists() else 0,
            }

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

        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target_path.with_name(f"{target_path.stem}.tmp{target_path.suffix}")

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
            "-i", str(source_path),
            "-ar", str(sample_rate),
            "-map_metadata", "0",
            "-f", fmt_flag,
        ]

        # Apply TPDF dithering if converting to 16-bit or resampled
        if dither:
            cmd.extend(["-dither_method", "triangular"])

        if target_format == TargetFormat.AIFF:
            codec = "pcm_s16be" if sample_depth == 16 else "pcm_s24be"
            cmd.extend(["-c:a", codec, "-write_id3v2", "1"])
        elif target_format == TargetFormat.WAV:
            codec = "pcm_s16le" if sample_depth == 16 else "pcm_s24le"
            cmd.extend(["-c:a", codec])
        elif target_format == TargetFormat.MP3:
            cmd.extend(["-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"])
        else:
            codec = "pcm_s16be"
            cmd.extend(["-c:a", codec])

        cmd.append(str(tmp_target))

        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                if tmp_target.exists():
                    tmp_target.unlink()
                return False, 0, f"FFmpeg error: {res.stderr.strip()}"

            if not tmp_target.exists() or tmp_target.stat().st_size == 0:
                return False, 0, "FFmpeg produced empty output file."

            os.replace(tmp_target, target_path)
            new_size = target_path.stat().st_size
            return True, new_size, None

        except Exception as e:
            if tmp_target.exists():
                tmp_target.unlink()
            return False, 0, str(e)
