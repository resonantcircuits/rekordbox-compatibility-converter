"""DeviceSQL parser and patcher for Rekordbox export.pdb."""

import os
import shutil
import struct
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

from .models import TrackInfo


TRACK_FILE_TYPE_OFFSET = 0x5A
DATABASE_SEQUENCE_OFFSET = 0x14
PAGE_SEQUENCE_OFFSET = 0x10


def device_sql_bitrate_kbps(bits_per_second: int) -> int:
    """Convert an audio bitrate to DeviceSQL's whole-kilobit representation."""
    return max(0, int(bits_per_second)) // 1000


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

        self.len_page, self.num_tables, _next_unused_page = struct.unpack_from(
            "<III", self.data, 4
        )
        self.seq_db = struct.unpack_from(
            "<I", self.data, DATABASE_SEQUENCE_OFFSET
        )[0]
        if self.len_page != 4096:
            raise ValueError(f"Unsupported DeviceSQL page size: {self.len_page} bytes.")
        if len(self.data) % self.len_page != 0:
            raise ValueError("PDB file length is not aligned to its page size.")
        if 0x1C + (self.num_tables * 16) > self.len_page:
            raise ValueError("PDB table directory extends beyond the header page.")

        page_sequences = [
            struct.unpack_from("<I", self.data, page_offset + PAGE_SEQUENCE_OFFSET)[0]
            for page_offset in range(self.len_page, len(self.data), self.len_page)
        ]
        max_page_sequence = max(page_sequences, default=-1)
        if self.seq_db <= max_page_sequence:
            raise ValueError(
                "PDB header sequence must be greater than every page sequence "
                f"(header {self.seq_db}, maximum page {max_page_sequence})."
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
        if str_pos < row_base + 0x88 or str_pos >= len(page_data):
            return "", 0, 0

        kind = page_data[str_pos]
        if kind == 0x40:
            # Long ASCII: [0x40, len_u2, type_u1, ...ascii]
            if str_pos + 4 > len(page_data):
                return "", 0, 0
            length = struct.unpack_from("<H", page_data, str_pos + 1)[0]
            if length < 4 or str_pos + length > len(page_data):
                return "", 0, 0
            text_bytes = page_data[str_pos + 4 : str_pos + length]
            return text_bytes.decode("ascii", errors="replace").rstrip("\x00"), str_pos, length
        elif kind == 0x90:
            # Long UTF-16LE: [0x90, len_u2, type_u1, ...utf16]
            if str_pos + 4 > len(page_data):
                return "", 0, 0
            length = struct.unpack_from("<H", page_data, str_pos + 1)[0]
            if length < 4 or (length - 4) % 2 or str_pos + length > len(page_data):
                return "", 0, 0
            text_bytes = page_data[str_pos + 4 : str_pos + length]
            return (
                text_bytes.decode("utf-16le", errors="replace").rstrip("\x00"),
                str_pos,
                length,
            )
        else:
            # Short ASCII: [(len << 1) | 1, ...ascii]
            if (kind & 1) == 0:
                return "", 0, 0
            length = kind >> 1
            if length <= 1:
                return "", str_pos, 1
            if str_pos + length > len(page_data):
                return "", 0, 0
            text_bytes = page_data[str_pos + 1 : str_pos + length]
            return (
                text_bytes.decode("ascii", errors="replace").rstrip("\x00"),
                str_pos,
                length,
            )

    def _encode_dsql_string(self, text: str, prefer_utf16: bool = False) -> bytearray:
        """Encodes a string into DeviceSQL binary format."""
        try:
            raw = text.encode("ascii")
        except UnicodeEncodeError:
            prefer_utf16 = True

        if prefer_utf16:
            raw_utf16 = text.encode("utf-16le")
            total_len = len(raw_utf16) + 4
            header = struct.pack("<BHB", 0x90, total_len, 0x03)
            return bytearray(header) + bytearray(raw_utf16)

        if len(raw) < 127:
            # Short ASCII: byte 0 is (len + 1) * 2 + 1
            total_len = len(raw) + 1
            header_byte = (total_len << 1) | 1
            return bytearray([header_byte]) + bytearray(raw)
        else:
            # Long ASCII: [0x40, len_u2, type_u1, ...ascii] to mirror _read_dsql_string
            total_len = len(raw) + 4
            header = struct.pack("<BHB", 0x40, total_len, 0x03)
            return bytearray(header) + bytearray(raw)

    def _string_alloc_size(self, abs_pos: int) -> int:
        """Returns the total encoded byte length of the existing string at abs_pos."""
        if abs_pos < 0 or abs_pos >= len(self.data):
            return 0
        kind = self.data[abs_pos]
        if kind in (0x40, 0x90):
            if abs_pos + 3 > len(self.data):
                return 0
            return struct.unpack_from("<H", self.data, abs_pos + 1)[0]
        return (kind >> 1) if (kind & 1) else 0

    def can_fit_strings(self, track: TrackInfo, new_filename: str, new_filepath: str) -> bool:
        """Checks whether both replacement strings fit inside the existing heap allocations."""
        if len(track.ofs_strings) <= 20 or track.page_idx < 0:
            return False
        page_start = track.page_idx * self.len_page
        page_end = page_start + self.len_page
        row_base = track.page_idx * self.len_page + track.row_offset
        if row_base < page_start + 0x28 or row_base + 0x88 > min(page_end, len(self.data)):
            return False
        for ofs, text in ((track.ofs_strings[19], new_filename), (track.ofs_strings[20], new_filepath)):
            if ofs == 0:
                return False
            pos = row_base + ofs
            if pos < row_base + 0x88 or pos >= page_end:
                return False
            prefer_utf16 = self.data[pos] == 0x90
            allocation = self._string_alloc_size(pos)
            if allocation <= 0 or pos + allocation > page_end:
                return False
            if len(self._encode_dsql_string(text, prefer_utf16)) > allocation:
                return False
        return True

    def _parse_tracks(self) -> List[TrackInfo]:
        """Parses all Track rows from Table 0."""
        track_table = next((t for t in self.tables if t["type"] == 0), None)
        if not track_table:
            return []

        tracks: List[TrackInfo] = []
        current_page_idx = track_table["first_page"]
        if current_page_idx == 0:
            return []
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
            if page_idx != current_page_idx:
                raise ValueError(
                    f"PDB page index mismatch: expected {current_page_idx}, found {page_idx}."
                )
            raw18 = struct.unpack_from("<I", page_data, 0x18)[0]
            num_row_offsets = raw18 & 0x1FFF
            num_rows = (raw18 >> 13) & 0x7FF
            page_flags = (raw18 >> 24) & 0xFF

            # If data page (flags & 0x40 == 0) and num_row_offsets > 0
            if (page_flags & 0x40) == 0 and num_row_offsets > 0 and num_rows > 0:
                num_groups = ((num_row_offsets - 1) // 16) + 1
                present_rows_seen = 0
                for g in range(num_groups):
                    if present_rows_seen >= num_rows:
                        break
                    base = self.len_page - (g * 0x24)
                    if base - 0x24 < 0x28:
                        break
                    row_present_flags = struct.unpack_from("<H", page_data, base - 4)[0]

                    for r in range(16):
                        if present_rows_seen >= num_rows:
                            break
                        row_global_idx = g * 16 + r
                        if row_global_idx >= num_row_offsets:
                            break
                        is_present = (row_present_flags >> r) & 1
                        if not is_present:
                            continue
                        present_rows_seen += 1

                        ofs_row = struct.unpack_from("<H", page_data, base - (6 + 2 * r))[0]
                        row_base = 0x28 + ofs_row

                        if row_base < 0x28 or row_base + 0x88 > self.len_page:
                            continue

                        sample_rate = struct.unpack_from("<I", page_data, row_base + 0x08)[0]
                        file_size = struct.unpack_from("<I", page_data, row_base + 0x10)[0]
                        bitrate = struct.unpack_from("<I", page_data, row_base + 0x30)[0]
                        track_id = struct.unpack_from("<I", page_data, row_base + 0x48)[0]
                        sample_depth = struct.unpack_from("<H", page_data, row_base + 0x52)[0]
                        duration = struct.unpack_from("<H", page_data, row_base + 0x54)[0]
                        file_type = struct.unpack_from(
                            "<H", page_data, row_base + TRACK_FILE_TYPE_OFFSET
                        )[0]

                        ofs_strings = struct.unpack_from("<21H", page_data, row_base + 0x5e)

                        title, _, _ = self._read_dsql_string(page_data, row_base, ofs_strings[17])
                        filename, filename_pos, _ = self._read_dsql_string(page_data, row_base, ofs_strings[19])
                        file_path, filepath_pos, _ = self._read_dsql_string(page_data, row_base, ofs_strings[20])
                        analyze_path, _, _ = self._read_dsql_string(page_data, row_base, ofs_strings[14])

                        if not filename or not file_path or not filename_pos or not filepath_pos:
                            continue

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
                                file_type=file_type,
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
        new_file_type: int,
    ) -> bool:
        """Updates a track record in the PDB buffer.

        Returns False (leaving the buffer untouched) if either replacement string
        is longer than the existing heap allocation — growing a string in place
        would overwrite adjacent heap data.
        """
        page_offset = track.page_idx * self.len_page
        row_base = page_offset + track.row_offset

        if not self.can_fit_strings(track, new_filename, new_filepath):
            return False
        if not 0 <= new_file_type <= 0xFFFF:
            return False

        # Update binary numerical fields
        struct.pack_into("<I", self.data, row_base + 0x08, new_sample_rate)
        struct.pack_into("<I", self.data, row_base + 0x10, new_filesize)
        struct.pack_into("<I", self.data, row_base + 0x30, new_bitrate)
        struct.pack_into("<H", self.data, row_base + 0x52, new_sample_depth)
        struct.pack_into(
            "<H", self.data, row_base + TRACK_FILE_TYPE_OFFSET, new_file_type
        )

        # Update filename (ofs_strings[19]) and filepath (ofs_strings[20]) in place
        for ofs, text in ((track.ofs_strings[19], new_filename), (track.ofs_strings[20], new_filepath)):
            if ofs != 0:
                pos = row_base + ofs
                allocation = self._string_alloc_size(pos)
                encoded = self._encode_dsql_string(text, self.data[pos] == 0x90)
                self.data[pos : pos + allocation] = encoded + (b"\x00" * (allocation - len(encoded)))

        # These are fixed-allocation, in-place row edits. DeviceSQL readers do not
        # require transaction bookkeeping changes for this operation, and
        # changing the header/page generations independently can make an
        # otherwise valid database appear corrupt to rekordbox.

        # Update track in memory
        track.filename = new_filename
        track.file_path = new_filepath
        track.file_size = new_filesize
        track.sample_rate = new_sample_rate
        track.sample_depth = new_sample_depth
        track.bitrate = new_bitrate
        track.file_type = new_file_type

        return True

    def update_track_bitrate(self, track: TrackInfo, new_bitrate: int) -> bool:
        """Update only the DeviceSQL bitrate field for a metadata-only repair."""
        if not 0 <= new_bitrate <= 0xFFFFFFFF:
            return False
        row_base = track.page_idx * self.len_page + track.row_offset
        if row_base < 0 or row_base + 0x34 > len(self.data):
            return False
        struct.pack_into("<I", self.data, row_base + 0x30, new_bitrate)
        track.bitrate = new_bitrate
        return True

    def save(self, backup: bool = True) -> Path:
        """Saves changes back to export.pdb atomically."""
        if backup and self.pdb_path.exists():
            backup_path = self.pdb_path.with_suffix(".pdb.bak")
            backup_temp = backup_path.with_name(
                f".{backup_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                shutil.copy2(self.pdb_path, backup_temp)
                with open(backup_temp, "r+b") as backup_file:
                    os.fsync(backup_file.fileno())
                os.replace(backup_temp, backup_path)
            finally:
                backup_temp.unlink(missing_ok=True)

        temp_path = self.pdb_path.with_name(
            f".{self.pdb_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(temp_path, "wb") as f:
                f.write(self.data)
                f.flush()
                os.fsync(f.fileno())

            # Atomically replace
            os.replace(temp_path, self.pdb_path)
        finally:
            temp_path.unlink(missing_ok=True)
        if os.name == "posix":
            try:
                directory_fd = os.open(str(self.pdb_path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return self.pdb_path
