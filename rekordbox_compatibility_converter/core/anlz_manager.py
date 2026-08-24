"""ANLZ parser and patcher for Rekordbox analysis files (.DAT / .EXT / .2EX)."""

import os
import shutil
import struct
import uuid
from pathlib import Path
from typing import Optional, Tuple


class ANLZManager:
    """Parses and updates audio path references in Rekordbox ANLZ files."""

    @staticmethod
    def read_path(anlz_file: Path) -> Optional[str]:
        """Reads the audio path from an ANLZ file's PPTH tag."""
        if not anlz_file.is_file():
            return None

        try:
            with open(anlz_file, "rb") as f:
                data = f.read()
        except OSError:
            return None

        if len(data) < 28 or data[:4] != b"PMAI":
            return None

        _, len_header, len_file = struct.unpack_from(">4sII", data, 0)
        if len_header != 28 or len_file != len(data):
            return None
        pos = len_header

        while pos + 12 <= len(data):
            fourcc, _, tag_len = struct.unpack_from(">4sII", data, pos)
            if tag_len < 12 or pos + tag_len > len(data):
                return None
            if fourcc == b"PPTH":
                if tag_len < 18 or pos + 16 > len(data):
                    return None
                hdr_len = struct.unpack_from(">I", data, pos + 4)[0]
                if hdr_len != 16:
                    return None
                len_path = struct.unpack_from(">I", data, pos + 12)[0]
                path_end = pos + 16 + len_path
                if len_path < 2 or len_path % 2 or path_end > pos + tag_len:
                    return None
                raw_bytes = data[pos + 16 : path_end]
                if raw_bytes[-2:] != b"\x00\x00":
                    return None
                try:
                    return raw_bytes[:-2].decode("utf-16be")
                except UnicodeDecodeError:
                    return None
            pos += tag_len

        return None

    @staticmethod
    def update_path(anlz_file: Path, new_audio_path: str, backup: bool = False) -> bool:
        """Updates the audio path in the PPTH tag and adjusts file lengths."""
        if not anlz_file.is_file():
            return False

        try:
            with open(anlz_file, "rb") as f:
                data = bytearray(f.read())
        except OSError:
            return False

        if len(data) < 28 or data[:4] != b"PMAI":
            return False

        _, len_header, len_file = struct.unpack_from(">4sII", data, 0)
        if len_header != 28 or len_file != len(data):
            return False
        pos = len_header
        ppth_found = False

        while pos + 12 <= len(data):
            fourcc, hdr_len, tag_len = struct.unpack_from(">4sII", data, pos)
            if tag_len < 12 or pos + tag_len > len(data):
                break
            if fourcc == b"PPTH":
                if tag_len < 18 or pos + 16 > len(data) or hdr_len != 16:
                    break
                ppth_found = True
                # Prepare new UTF-16BE path
                try:
                    encoded_path = new_audio_path.encode("utf-16be") + b"\x00\x00"
                except UnicodeEncodeError:
                    return False
                new_len_path = len(encoded_path)
                # The PPTH body is exactly the UTF-16BE path, including its
                # terminating NUL. Real Rekordbox files do not align this tag
                # to a four-byte boundary; adding padding shifts every
                # following waveform tag and can make hardware reject it.
                new_tag_len = 16 + new_len_path

                # Construct new PPTH section
                new_ppth = struct.pack(">4sIII", b"PPTH", hdr_len, new_tag_len, new_len_path) + encoded_path

                # Replace section in data
                data[pos : pos + tag_len] = new_ppth

                # PMAI len_file is at offset 0x08 and should describe the
                # actual rewritten byte length, even if the old value was stale.
                struct.pack_into(">I", data, 8, len(data))
                break

            pos += tag_len

        if not ppth_found:
            return False

        if backup:
            bak_path = anlz_file.with_suffix(anlz_file.suffix + ".bak")
            bak_temp = bak_path.with_name(f".{bak_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copy2(anlz_file, bak_temp)
                with open(bak_temp, "rb") as backup_file:
                    os.fsync(backup_file.fileno())
                os.replace(bak_temp, bak_path)
            finally:
                bak_temp.unlink(missing_ok=True)

        tmp_path = anlz_file.with_name(f".{anlz_file.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, anlz_file)
        finally:
            tmp_path.unlink(missing_ok=True)
        if os.name == "posix":
            try:
                directory_fd = os.open(str(anlz_file.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return True
