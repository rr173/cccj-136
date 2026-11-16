"""Page checker: verifies an archive end to end.

For every row group and every leaf column it
  * reads and CRC-verifies every page (struct, values, row-index),
  * checks the physical layout tiles the row-group region contiguously,
  * fully decodes the struct stream row by row, recomputing each row's
    struct/value stream offsets and comparing them against the row index
    (boundary check),
  * counts VALUE tokens and compares with the number of values decoded from
    the value stream and with the count stored in the meta (leaf-count check),
  * verifies both streams are consumed exactly.

Exit code 0 and "CHECK OK" only if everything holds.
"""

import struct

from .encoding import BytesReader
from .errors import IndexCorrupt, PageCorrupt
from .scanner import walk_column


class _CountVisitor:
    def __init__(self):
        self.values = 0

    def missing(self, pos, lv):
        pass

    def null(self, pos, lv):
        pass

    def empty(self, pos, lv):
        pass

    def struct_begin(self, pos, lv):
        pass

    def struct_end(self, pos, lv):
        pass

    def list_begin(self, pos, lv):
        pass

    def list_end(self, pos, lv):
        pass

    def value(self, pos, lv, v):
        self.values += 1

    def value_skipped(self, pos, lv):
        pass


def _read_whole_stream(archive, rg_index, col_path, which, stream):
    chunks = []
    for idx in range(len(stream.pages)):
        chunks.append(stream._load_page(idx))
    return b"".join(chunks)


def check_archive(archive, out):
    problems = []
    info_lines = []

    def problem(msg):
        problems.append(msg)

    # verify row groups tile the file between header and footer
    header_end = 12 + len(_header_json(archive)) + 4
    cursor = header_end
    for rg in archive.row_groups:
        if rg["offset"] != cursor:
            problem("row group %d starts at %d, expected %d"
                    % (rg["index"], rg["offset"], cursor))
        cursor = rg["meta_offset"] + rg["meta_len"]
    footer_start = archive.size - 16 - _footer_json_len(archive)
    if cursor != footer_start:
        problem("row groups end at %d, footer starts at %d" % (cursor, footer_start))

    for rg_index in range(len(archive.row_groups)):
        meta = archive.rg_meta(rg_index)      # raises IndexCorrupt on crc fail
        rg = archive.row_groups[rg_index]
        # layout: pages of all columns may be interleaved (the builder flushes
        # buffers as they fill); the set of records must tile the row-group
        # region [rg.offset, rg.meta_offset) exactly.
        col_entries = sorted(meta["columns"].items(), key=lambda kv: kv[1]["id"])
        records = []
        for col_path, colmeta in col_entries:
            records += [tuple(p) for p in colmeta["struct"]["pages"]]
            records += [tuple(p) for p in colmeta["values"]["pages"]]
            records.append(tuple(colmeta["row_index"]))
        records.sort()
        cursor = rg["offset"]
        for (off, ln) in records:
            if off != cursor:
                problem("rg=%d record gap/overlap at %d (expected %d)"
                        % (rg_index, off, cursor))
                break
            cursor = off + ln
        if cursor != rg["meta_offset"]:
            problem("rg=%d meta starts at %d, records end at %d"
                    % (rg_index, rg["meta_offset"], cursor))

        # per-column stream verification
        for col_path, colmeta in col_entries:
            try:
                _check_column(archive, rg_index, col_path, colmeta,
                              meta["row_count"], info_lines)
            except (IndexCorrupt, PageCorrupt) as exc:
                problem("rg=%d col=%s: %s" % (rg_index, col_path, exc))

    for line in info_lines:
        out.write(line + "\n")
    if problems:
        for p in problems:
            out.write("CHECK FAIL: %s\n" % p)
        out.write("CHECK FAIL: %d problem(s)\n" % len(problems))
        return False
    out.write("CHECK OK: %d row group(s), %d rows, %d column(s) verified\n"
              % (len(archive.row_groups), archive.total_rows,
                 len(archive.schema.columns)))
    return True


def _header_json(archive):
    (hlen,) = struct.unpack("<I", archive._read_at(8, 4))
    return archive._read_at(12, hlen)


def _footer_json_len(archive):
    (flen,) = struct.unpack("<I", archive._read_at(archive.size - 12, 4))
    return flen


def _check_column(archive, rg_index, col_path, colmeta, row_count, info_lines):
    col = archive.schema.column_by_path(col_path)
    s_offs, v_offs = archive.row_index(rg_index, col_path)   # crc-verified
    struct_stream = archive.stream(rg_index, col_path, "struct")
    value_stream = archive.stream(rg_index, col_path, "values")
    struct_bytes = _read_whole_stream(archive, rg_index, col_path, "struct",
                                      struct_stream)
    value_bytes = _read_whole_stream(archive, rg_index, col_path, "values",
                                     value_stream)
    if len(struct_bytes) != colmeta["struct"]["bytes"]:
        raise PageCorrupt("struct stream size mismatch")
    if len(value_bytes) != colmeta["values"]["bytes"]:
        raise PageCorrupt("value stream size mismatch")

    tr = BytesReader(struct_bytes)
    vr = BytesReader(value_bytes)
    total_values = 0
    for i in range(row_count):
        if tr.pos != s_offs[i] or vr.pos != v_offs[i]:
            raise PageCorrupt(
                "row %d boundary mismatch: decoded (%d,%d), row index (%d,%d)"
                % (i, tr.pos, vr.pos, s_offs[i], v_offs[i]))
        vis = _CountVisitor()
        walk_column(col.levels, tr, vr, vis)
        total_values += vis.values
    if not tr.at_end() or not vr.at_end():
        raise PageCorrupt("streams not fully consumed after %d rows" % row_count)
    if total_values != colmeta["values"]["count"]:
        raise PageCorrupt("leaf count mismatch: %d VALUE tokens vs meta count %d"
                          % (total_values, colmeta["values"]["count"]))
    info_lines.append(
        "INFO rg=%d col=%s rows=%d struct_bytes=%d value_bytes=%d "
        "leaves=%d struct_pages=%d value_pages=%d"
        % (rg_index, col_path, row_count, len(struct_bytes), len(value_bytes),
           total_values, len(struct_stream.pages), len(value_stream.pages)))
