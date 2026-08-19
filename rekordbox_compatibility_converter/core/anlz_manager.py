"""ANLZ parser and patcher for Rekordbox analysis files (.DAT / .EXT)."""

import os
import shutil
import struct
from pathlib import Path
from typing import Optional, Tuple


class ANLZManager:
    """Parses and updates audio path references in Rekordbox ANLZ files."""

    @staticmethod
    def read_path(anlz_file: Path) -> Optional[str]:
        """Reads the audio path from an ANLZ file's PPTH tag."""
        if not anlz_file.exists():
            return None

        with open(anlz_file, "rb") as f:
            data = f.read()

        if len(data) < 28 or data[:4] != b"PMAI":
            return None

        magic, len_header, len_file = struct.unpack_from(">4sII", data, 0)
        pos = len_header

        while pos + 12 <= len(data):
            fourcc, hdr_len, tag_len = struct.unpack_from(">4sII", data, pos)
            if tag_len < 12:
                return None
            if fourcc == b"PPTH":
                len_path = struct.unpack_from(">I", data, pos + 12)[0]
                if len_path > 2:
                    raw_bytes = data[pos + 16 : pos + 16 + len_path - 2]
                    return raw_bytes.decode("utf-16be", errors="replace")
                return ""
            pos += tag_len

        return None

    @staticmethod
    def update_path(anlz_file: Path, new_audio_path: str, backup: bool = False) -> bool:
        """Updates the audio path in the PPTH tag and adjusts file lengths."""
        if not anlz_file.exists():
            return False

        with open(anlz_file, "rb") as f:
            data = bytearray(f.read())

        if len(data) < 28 or data[:4] != b"PMAI":
            return False

        magic, len_header, len_file = struct.unpack_from(">4sII", data, 0)
        pos = len_header
        ppth_found = False

        while pos + 12 <= len(data):
            fourcc, hdr_len, tag_len = struct.unpack_from(">4sII", data, pos)
            if tag_len < 12:
                break
            if fourcc == b"PPTH":
                ppth_found = True
                # Prepare new UTF-16BE path
                encoded_path = new_audio_path.encode("utf-16be") + b"\x00\x00"
                new_len_path = len(encoded_path)
                new_tag_len = 12 + 4 + new_len_path

                # Align to 4-byte boundary if needed
                pad_bytes = (4 - (new_tag_len % 4)) % 4
                encoded_path += b"\x00" * pad_bytes
                new_len_path += pad_bytes
                new_tag_len += pad_bytes

                # Calculate length delta
                delta = new_tag_len - tag_len

                # Construct new PPTH section
                new_ppth = struct.pack(">4sIII", b"PPTH", hdr_len, new_tag_len, new_len_path) + encoded_path

                # Replace section in data
                data[pos : pos + tag_len] = new_ppth

                # Update file header length
                new_len_file = len_file + delta
                struct.pack_into(">I", data, 8, new_len_file)
                break

            pos += tag_len

        if not ppth_found:
            return False

        if backup:
            bak_path = anlz_file.with_suffix(anlz_file.suffix + ".bak")
            shutil.copy2(anlz_file, bak_path)

        tmp_path = anlz_file.with_suffix(anlz_file.suffix + ".tmp")
        with open(tmp_path, "wb") as f:
            f.write(data)

        os.replace(tmp_path, anlz_file)
        return True
