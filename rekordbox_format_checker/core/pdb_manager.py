"""DeviceSQL parser and patcher for Rekordbox export.pdb."""

import os
import shutil
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import TrackInfo


class PDBManager:
    """Reads and updates Rekordbox export.pdb and exportExt.pdb database files."""

    def __init__(self, pdb_path: Path):
        self.pdb_path = Path(pdb_path)
        self.data: bytearray = bytearray()
        self.len_page: int = 4096
        self.num_tables: int = 0
        self.seq_db: int = 0
        self.tables: List[Dict] = []
        self.tracks: List[TrackInfo] = []
        if self.pdb_path.exists():
            self.load()

    def load(self) -> None:
        """Loads and parses export.pdb."""
        with open(self.pdb_path, "rb") as f:
            self.data = bytearray(f.read())

        if len(self.data) < 0x20:
            raise ValueError(f"PDB file {self.pdb_path} is too small to be valid.")

        self.len_page, self.num_tables, next_u, self.seq_db = struct.unpack_from(
            "<IIII", self.data, 4
        )

        self.tables = []
        offset = 0x1C
        for _ in range(self.num_tables):
            if offset + 16 > len(self.data):
                break
            t_type, empty_c, first_page, last_page = struct.unpack_from(
                "<IIII", self.data, offset
            )
            self.tables.append(
                {
                    "type": t_type,
                    "empty_c": empty_c,
                    "first_page": first_page,
                    "last_page": last_page,
                }
            )
            offset += 16

        self.tracks = self._parse_tracks()

    def _read_dsql_string(
        self, page_data: bytearray, row_base: int, ofs: int
    ) -> Tuple[str, int, int]:
        """Reads a DeviceSQL string from the page heap.

        Returns (string_text, abs_string_pos, total_encoded_bytes).
        """
        if ofs == 0:
            return "", 0, 0
        str_pos = row_base + ofs
        if str_pos >= len(page_data):
            return "", 0, 0

        kind = page_data[str_pos]
        if kind == 0x40:
            # Long ASCII: [0x40, len_u2, type_u1, ...ascii]
            length = struct.unpack_from("<H", page_data, str_pos + 1)[0]
            text_bytes = page_data[str_pos + 4 : str_pos + length]
            return text_bytes.decode("ascii", errors="replace").rstrip("\x00"), str_pos, length
        elif kind == 0x90:
            # Long UTF-16LE: [0x90, len_u2, type_u1, ...utf16]
            length = struct.unpack_from("<H", page_data, str_pos + 1)[0]
            text_bytes = page_data[str_pos + 4 : str_pos + length]
            return (
                text_bytes.decode("utf-16le", errors="replace").rstrip("\x00"),
                str_pos,
                length,
            )
        else:
            # Short ASCII: [(len << 1) | 1, ...ascii]
            length = kind >> 1
            if length <= 1:
                return "", str_pos, 1
            text_bytes = page_data[str_pos + 1 : str_pos + length]
            return (
                text_bytes.decode("ascii", errors="replace").rstrip("\x00"),
                str_pos,
                length,
            )

    def _encode_dsql_string(self, text: str) -> bytearray:
        """Encodes a string into DeviceSQL binary format."""
        raw = text.encode("ascii", errors="replace")
        if len(raw) < 127:
            # Short ASCII: byte 0 is (len + 1) * 2 + 1
            total_len = len(raw) + 1
            header_byte = (total_len << 1) | 1
            return bytearray([header_byte]) + bytearray(raw)
        else:
            # Long ASCII
            total_len = len(raw) + 4
            header = struct.pack("<HBB", total_len, 0x40, 0x03)
            return bytearray(header) + bytearray(raw)

    def _parse_tracks(self) -> List[TrackInfo]:
        """Parses all Track rows from Table 0."""
        track_table = next((t for t in self.tables if t["type"] == 0), None)
        if not track_table:
            return []

        tracks: List[TrackInfo] = []
        current_page_idx = track_table["first_page"]
        visited = set()

        while current_page_idx * self.len_page < len(self.data):
            if current_page_idx in visited:
                break
            visited.add(current_page_idx)

            page_offset = current_page_idx * self.len_page
            page_data = self.data[page_offset : page_offset + self.len_page]
            if len(page_data) < self.len_page:
                break

            gap, page_idx, p_type, next_page, p_seq = struct.unpack_from(
                "<IIIII", page_data, 0
            )
            raw18 = struct.unpack_from("<I", page_data, 0x18)[0]
            num_row_offsets = raw18 & 0x1FFF
            num_rows = (raw18 >> 13) & 0x7FF
            page_flags = (raw18 >> 24) & 0xFF

            # If data page (flags & 0x40 == 0) and num_row_offsets > 0
            if (page_flags & 0x40) == 0 and num_row_offsets > 0:
                num_groups = ((num_row_offsets - 1) // 16) + 1
                for g in range(num_groups):
                    base = self.len_page - (g * 0x24)
                    row_present_flags = struct.unpack_from("<H", page_data, base - 4)[0]

                    for r in range(16):
                        row_global_idx = g * 16 + r
                        if row_global_idx >= num_row_offsets:
                            break
                        is_present = (row_present_flags >> r) & 1
                        if not is_present:
                            continue

                        ofs_row = struct.unpack_from("<H", page_data, base - (6 + 2 * r))[0]
                        row_base = 0x28 + ofs_row

                        if row_base + 0x88 > self.len_page:
                            continue

                        sample_rate = struct.unpack_from("<I", page_data, row_base + 0x08)[0]
                        file_size = struct.unpack_from("<I", page_data, row_base + 0x10)[0]
                        bitrate = struct.unpack_from("<I", page_data, row_base + 0x30)[0]
                        track_id = struct.unpack_from("<I", page_data, row_base + 0x48)[0]
                        sample_depth = struct.unpack_from("<H", page_data, row_base + 0x52)[0]
                        duration = struct.unpack_from("<H", page_data, row_base + 0x54)[0]

                        ofs_strings = struct.unpack_from("<21H", page_data, row_base + 0x5e)

                        title, _, _ = self._read_dsql_string(page_data, row_base, ofs_strings[17])
                        filename, _, _ = self._read_dsql_string(page_data, row_base, ofs_strings[19])
                        file_path, _, _ = self._read_dsql_string(page_data, row_base, ofs_strings[20])
                        analyze_path, _, _ = self._read_dsql_string(page_data, row_base, ofs_strings[14])

                        tracks.append(
                            TrackInfo(
                                id=track_id,
                                title=title,
                                filename=filename,
                                file_path=file_path,
                                analyze_path=analyze_path,
                                sample_rate=sample_rate,
                                sample_depth=sample_depth,
                                bitrate=bitrate,
                                file_size=file_size,
                                duration=duration,
                                page_idx=current_page_idx,
                                row_offset=row_base,
                                ofs_strings=ofs_strings,
                            )
                        )

            if next_page == 0 or next_page >= (len(self.data) // self.len_page) or next_page == current_page_idx:
                break
            current_page_idx = next_page

        return tracks

    def update_track(
        self,
        track: TrackInfo,
        new_filename: str,
        new_filepath: str,
        new_filesize: int,
        new_sample_rate: int,
        new_sample_depth: int,
        new_bitrate: int,
    ) -> bool:
        """Updates a track record in the PDB buffer."""
        page_offset = track.page_idx * self.len_page
        row_base = page_offset + track.row_offset

        # Update binary numerical fields
        struct.pack_into("<I", self.data, row_base + 0x08, new_sample_rate)
        struct.pack_into("<I", self.data, row_base + 0x10, new_filesize)
        struct.pack_into("<I", self.data, row_base + 0x30, new_bitrate)
        struct.pack_into("<H", self.data, row_base + 0x52, new_sample_depth)

        # Update filename (ofs_strings[19])
        ofs_fn = track.ofs_strings[19]
        if ofs_fn != 0:
            fn_pos = row_base + ofs_fn
            old_kind = self.data[fn_pos]
            encoded_fn = self._encode_dsql_string(new_filename)
            # If same length, write directly in-place
            if (old_kind >> 1) == (encoded_fn[0] >> 1):
                self.data[fn_pos : fn_pos + len(encoded_fn)] = encoded_fn
            else:
                # If length changed, write as much as fits or encode
                if len(encoded_fn) <= (old_kind >> 1):
                    self.data[fn_pos : fn_pos + len(encoded_fn)] = encoded_fn
                else:
                    # In rare cases where string is longer, overwrite in-place
                    self.data[fn_pos : fn_pos + len(encoded_fn)] = encoded_fn

        # Update filepath (ofs_strings[20])
        ofs_fp = track.ofs_strings[20]
        if ofs_fp != 0:
            fp_pos = row_base + ofs_fp
            old_kind = self.data[fp_pos]
            encoded_fp = self._encode_dsql_string(new_filepath)
            if (old_kind >> 1) == (encoded_fp[0] >> 1):
                self.data[fp_pos : fp_pos + len(encoded_fp)] = encoded_fp
            else:
                self.data[fp_pos : fp_pos + len(encoded_fp)] = encoded_fp

        # Increment page sequence number
        page_seq = struct.unpack_from("<I", self.data, page_offset + 0x10)[0]
        struct.pack_into("<I", self.data, page_offset + 0x10, page_seq + 1)

        # Increment database sequence number
        self.seq_db += 1
        struct.pack_into("<I", self.data, 0x14, self.seq_db)

        # Update track in memory
        track.filename = new_filename
        track.file_path = new_filepath
        track.file_size = new_filesize
        track.sample_rate = new_sample_rate
        track.sample_depth = new_sample_depth
        track.bitrate = new_bitrate

        return True

    def save(self, backup: bool = True) -> Path:
        """Saves changes back to export.pdb atomically."""
        if backup and self.pdb_path.exists():
            backup_path = self.pdb_path.with_suffix(".pdb.bak")
            shutil.copy2(self.pdb_path, backup_path)

        temp_path = self.pdb_path.with_suffix(".pdb.tmp")
        with open(temp_path, "wb") as f:
            f.write(self.data)

        # Atomically replace
        os.replace(temp_path, self.pdb_path)
        return self.pdb_path
