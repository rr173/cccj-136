"""Archive reader: header/footer validation, row-group meta, row index and
paged stream access.

Every page/record actually read from disk is reported through the page log
(callable receiving a text line).  The log is factual -- one line per read,
never an estimate.

The archive is opened read-only and never modified.
"""

import bisect
import json
import struct

from . import layout
from .encoding import crc32, get_uvarint, BytesReader
from .errors import IndexCorrupt, PageCorrupt, SchemaError
from .schema import Schema


class Archive:
    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda line: None)
        self.f = open(path, "rb")           # read-only: the archive is never written
        self._rg_meta_cache = {}
        self._open_and_validate()

    # ---------------------------------------------------------- open/close
    def _read_at(self, off, n):
        self.f.seek(off)
        data = self.f.read(n)
        if len(data) != n:
            raise IndexCorrupt("short read at offset %d (truncated archive?)" % off)
        return data

    def _open_and_validate(self):
        self.f.seek(0, 2)
        self.size = self.f.tell()
        if self.size < len(layout.MAGIC_H) + len(layout.MAGIC_F) + 4:
            raise IndexCorrupt("file too small to be a nestedcol archive")
        if self._read_at(0, 8) != layout.MAGIC_H:
            raise IndexCorrupt("bad header magic (not a nestedcol archive)")
        (hlen,) = struct.unpack("<I", self._read_at(8, 4))
        header_json = self._read_at(12, hlen)
        (hcrc,) = struct.unpack("<I", self._read_at(12 + hlen, 4))
        if crc32(header_json) != hcrc:
            raise IndexCorrupt("header checksum mismatch")
        self.log("LOG HEADER off=8 len=%d crc=ok" % (hlen + 4))
        try:
            self.header = json.loads(header_json.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise IndexCorrupt("header JSON undecodable") from exc
        if self.header.get("format") != layout.FORMAT_VERSION:
            raise IndexCorrupt("unsupported format version %r"
                               % (self.header.get("format"),))
        try:
            self.schema = Schema.from_dict(self.header["schema"])
        except SchemaError:
            raise
        except Exception as exc:
            raise IndexCorrupt("schema in header is invalid: %s" % exc)

        if self._read_at(self.size - 8, 8) != layout.MAGIC_F:
            raise IndexCorrupt("bad tail magic (truncated or corrupt archive)")
        # footer layout: footer_json | u32 crc | u32 json_len | MAGIC_F
        (flen,) = struct.unpack("<I", self._read_at(self.size - 12, 4))
        if flen > self.size - 16 - 8:
            raise IndexCorrupt("footer length out of bounds")
        footer_json = self._read_at(self.size - 16 - flen, flen)
        (fcrc,) = struct.unpack("<I", self._read_at(self.size - 16, 4))
        if crc32(footer_json) != fcrc:
            raise IndexCorrupt("footer checksum mismatch")
        self.log("LOG FOOTER off=%d len=%d crc=ok" % (self.size - 16 - flen, flen))
        try:
            self.footer = json.loads(footer_json.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise IndexCorrupt("footer JSON undecodable") from exc
        if self.footer.get("format") != layout.FORMAT_VERSION:
            raise IndexCorrupt("unsupported footer format %r")
        schema_crc = crc32(json.dumps(self.header["schema"], sort_keys=True,
                                      separators=(",", ":")).encode("utf-8"))
        if self.footer.get("schema_crc32") != schema_crc:
            raise IndexCorrupt("footer/header schema checksum mismatch")
        self.row_groups = self.footer.get("row_groups")
        if not isinstance(self.row_groups, list):
            raise IndexCorrupt("footer row_groups missing")
        for rg in self.row_groups:
            for key in ("index", "offset", "row_count", "meta_offset", "meta_len"):
                if not isinstance(rg.get(key), int):
                    raise IndexCorrupt("footer row group entry invalid")
            if rg["meta_offset"] + rg["meta_len"] > self.size:
                raise IndexCorrupt("row group meta out of bounds")
        self.total_rows = self.footer.get("total_rows", 0)

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --------------------------------------------------------- row groups
    def rg_meta(self, rg_index):
        if rg_index in self._rg_meta_cache:
            return self._rg_meta_cache[rg_index]
        rg = self.row_groups[rg_index]
        off, mlen = rg["meta_offset"], rg["meta_len"]
        raw = self._read_at(off, mlen)
        if len(raw) < 8:
            raise IndexCorrupt("row group %d meta record too short" % rg_index)
        (jlen,) = struct.unpack("<I", raw[:4])
        if jlen != mlen - 8:
            raise IndexCorrupt("row group %d meta length mismatch" % rg_index)
        meta_json = raw[4:4 + jlen]
        (mcrc,) = struct.unpack("<I", raw[4 + jlen:4 + jlen + 4])
        if crc32(meta_json) != mcrc:
            raise IndexCorrupt("row group %d meta checksum mismatch" % rg_index)
        self.log("LOG META rg=%d off=%d len=%d crc=ok" % (rg_index, off, mlen))
        try:
            meta = json.loads(meta_json.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise IndexCorrupt("row group %d meta JSON undecodable" % rg_index) from exc
        if meta.get("row_count") != rg["row_count"]:
            raise IndexCorrupt("row group %d row count mismatch" % rg_index)
        self._rg_meta_cache[rg_index] = meta
        return meta

    def row_index(self, rg_index, col_path):
        """Returns (struct_offsets, value_offsets): one offset per row, into
        the column's logical streams.  This is boundary information only."""
        meta = self.rg_meta(rg_index)
        try:
            colmeta = meta["columns"][col_path]
            off, length = colmeta["row_index"]
        except (KeyError, TypeError) as exc:
            raise IndexCorrupt("row group %d: no row index for column %s"
                               % (rg_index, col_path)) from exc
        rec = self._read_at(off, length)
        try:
            kind, payload = _parse_page_record(rec, layout.PAGE_ROW_INDEX, 0)
        except PageCorrupt as exc:
            raise IndexCorrupt("row index record invalid (rg=%d col=%s): %s"
                               % (rg_index, col_path, exc)) from exc
        if crc32(rec[:9] + payload) != struct.unpack("<I", rec[9:13])[0]:
            raise IndexCorrupt("row index checksum mismatch (rg=%d col=%s)"
                               % (rg_index, col_path))
        self.log("LOG INDEX rg=%d col=%s off=%d len=%d crc=ok"
                 % (rg_index, col_path, off, length))
        r = BytesReader(payload)
        n = get_uvarint(r)
        row_count = self.row_groups[rg_index]["row_count"]
        if n != row_count:
            raise IndexCorrupt("row index count mismatch (rg=%d col=%s)"
                               % (rg_index, col_path))
        s_offs, v_offs = [], []
        s = v = 0
        for _ in range(n):
            s += get_uvarint(r)
            v += get_uvarint(r)
            s_offs.append(s)
            v_offs.append(v)
        if r.remaining() != 0:
            raise IndexCorrupt("row index has trailing bytes (rg=%d col=%s)"
                               % (rg_index, col_path))
        # boundary sanity: monotonic and within the streams
        if s_offs != sorted(s_offs) or v_offs != sorted(v_offs):
            raise IndexCorrupt("row index offsets not monotonic (rg=%d col=%s)"
                               % (rg_index, col_path))
        if n and (s_offs[-1] > colmeta["struct"]["bytes"]
                  or v_offs[-1] > colmeta["values"]["bytes"]):
            raise IndexCorrupt("row index offset beyond stream (rg=%d col=%s)"
                               % (rg_index, col_path))
        return s_offs, v_offs

    # ------------------------------------------------------------ streams
    def stream(self, rg_index, col_path, which):
        """which: 'struct' or 'values' -> PagedStream."""
        meta = self.rg_meta(rg_index)
        try:
            smeta = meta["columns"][col_path][which]
        except (KeyError, TypeError) as exc:
            raise IndexCorrupt("row group %d: no %s stream for column %s"
                               % (rg_index, which, col_path)) from exc
        pages = [tuple(p) for p in smeta["pages"]]
        starts = []
        acc = 0
        for (_, ln) in pages:
            starts.append(acc)
            acc += ln - layout.PAGE_HEADER_LEN
        if acc != smeta["bytes"]:
            raise IndexCorrupt("row group %d col %s %s: page directory size "
                               "mismatch" % (rg_index, col_path, which))
        kind = layout.PAGE_STRUCT if which == "struct" else layout.PAGE_VALUES
        return PagedStream(self, rg_index, col_path, which, kind, pages,
                           starts, acc)


def _parse_page_record(rec, want_kind, want_index):
    if len(rec) < layout.PAGE_HEADER_LEN:
        raise PageCorrupt("page record too short")
    kind = rec[0]
    index, plen = struct.unpack("<II", rec[1:9])
    if kind != want_kind or index != want_index:
        raise PageCorrupt("page header mismatch (kind=%d index=%d)"
                          % (kind, index))
    if plen != len(rec) - layout.PAGE_HEADER_LEN:
        raise PageCorrupt("page payload length mismatch")
    return kind, rec[layout.PAGE_HEADER_LEN:]


class PagedStream:
    """A logical byte stream chunked into page records.  Reads are lazy:
    only the pages covering a requested range are fetched (and logged)."""

    def __init__(self, archive, rg_index, col_path, which, kind, pages,
                 starts, total_bytes):
        self.archive = archive
        self.rg_index = rg_index
        self.col_path = col_path
        self.which = which
        self.kind = kind
        self.pages = pages            # list of (file offset, record length)
        self.starts = starts          # logical start offset of each page
        self.total_bytes = total_bytes

    def reader(self, start, end):
        if not (0 <= start <= end <= self.total_bytes):
            raise PageCorrupt("range [%d,%d) outside %s stream of %s"
                              % (start, end, self.which, self.col_path))
        return StreamReader(self, start, end)

    def _load_page(self, idx):
        off, ln = self.pages[idx]
        a = self.archive
        a.f.seek(off)
        rec = a.f.read(ln)
        if len(rec) != ln:
            raise PageCorrupt("short read of page %d (rg=%d col=%s %s)"
                              % (idx, self.rg_index, self.col_path, self.which))
        kind, payload = _parse_page_record(rec, self.kind, idx)
        (crc_stored,) = struct.unpack("<I", rec[9:13])
        if crc32(rec[:9] + payload) != crc_stored:
            raise PageCorrupt("page checksum mismatch (rg=%d col=%s %s page=%d)"
                              % (self.rg_index, self.col_path, self.which, idx))
        a.log("LOG PAGE rg=%d col=%s stream=%s page=%d off=%d len=%d crc=ok"
              % (self.rg_index, self.col_path, self.which, idx, off, ln))
        return payload


class StreamReader:
    """Reader over a logical range of a PagedStream.  Holds at most one
    page payload in memory."""

    def __init__(self, stream, start, end):
        self.stream = stream
        self.pos = start
        self.end = end
        self.page_idx = -1
        self.page_buf = b""
        self.page_start = 0           # logical offset of page_buf[0]

    def _ensure(self):
        s = self.stream
        if self.page_idx >= 0 and self.page_start <= self.pos < self.page_start + len(self.page_buf):
            return
        idx = bisect.bisect_right(s.starts, self.pos) - 1
        if idx < 0 or idx >= len(s.pages):
            raise PageCorrupt("no page covers offset %d (rg=%d col=%s %s)"
                              % (self.pos, s.rg_index, s.col_path, s.which))
        self.page_buf = s._load_page(idx)
        self.page_idx = idx
        self.page_start = s.starts[idx]

    def read(self, n):
        if self.pos + n > self.end:
            raise PageCorrupt("read past end of row range (rg=%d col=%s %s)"
                              % (self.stream.rg_index, self.stream.col_path,
                                  self.stream.which))
        out = bytearray()
        while n > 0:
            self._ensure()
            in_page = self.pos - self.page_start
            avail = len(self.page_buf) - in_page
            if avail <= 0:
                raise PageCorrupt("stream page layout error")
            take = min(avail, n)
            out += self.page_buf[in_page:in_page + take]
            self.pos += take
            n -= take
        return bytes(out)

    def read_byte(self):
        if self.pos >= self.end:
            raise PageCorrupt("read past end of row range (rg=%d col=%s %s)"
                              % (self.stream.rg_index, self.stream.col_path,
                                  self.stream.which))
        self._ensure()
        b = self.page_buf[self.pos - self.page_start]
        self.pos += 1
        return b

    def at_end(self):
        return self.pos >= self.end
