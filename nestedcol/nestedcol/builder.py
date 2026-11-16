"""Builder: JSONL -> nestedcol archive.

The builder consumes SAX events from ``jsonsax`` and incrementally appends
structure tokens and leaf values to per-column stream buffers.  Buffers are
flushed to page records as soon as they reach ``page_size`` bytes, so memory
use stays bounded regardless of how large a single root row grows:

  * JSON parsing buffers: one chunk (64 KiB) + at most one scalar
    (``max_scalar`` bytes, the only public size limit);
  * column stream buffers: at most ``page_size`` bytes each;
  * row-index arrays: two int64 per row of the *current row group*.

A single root row may therefore span arbitrarily many pages; there is no
limit on list lengths or row sizes other than the single-scalar cap.
"""

import json
import struct
from array import array

from . import jsonsax, layout
from .encoding import (INT64_MAX, INT64_MIN, TOK_BEGIN, TOK_EMPTY, TOK_END,
                       TOK_MISS, TOK_NULL, TOK_PRESENT, TOK_VALUE, crc32,
                       encode_value, put_uvarint)
from .errors import DataError
from .schema import LEAF_KINDS

_INT_RE_CHARS = set("0123456789")


def _write_page_record(out, kind, index, payload):
    hdr = bytes([kind]) + struct.pack("<II", index, len(payload))
    crc = crc32(hdr + payload)
    off = out.tell()
    out.write(hdr)
    out.write(struct.pack("<I", crc))
    out.write(payload)
    return (off, layout.PAGE_HEADER_LEN + len(payload))


class _ColumnState:
    __slots__ = ("struct_buf", "value_buf", "struct_pages", "value_pages",
                 "flushed_struct", "flushed_values", "value_count",
                 "row_struct_off", "row_value_off")

    def __init__(self):
        self.struct_buf = bytearray()
        self.value_buf = bytearray()
        self.struct_pages = []          # list of (offset, length)
        self.value_pages = []
        self.flushed_struct = 0         # logical bytes already paged out
        self.flushed_values = 0
        self.value_count = 0
        self.row_struct_off = array("q")   # per row: struct stream offset
        self.row_value_off = array("q")    # per row: value stream offset

    @property
    def struct_bytes(self):
        return self.flushed_struct + len(self.struct_buf)

    @property
    def value_bytes(self):
        return self.flushed_values + len(self.value_buf)


class _RowGroupWriter:
    """Accumulates encoded rows and writes one row group on finish()."""

    def __init__(self, out, schema, page_size):
        self.out = out
        self.schema = schema
        self.page_size = page_size
        self.cols = {c.id: _ColumnState() for c in schema.columns}
        self.col_by_id = {c.id: c for c in schema.columns}
        self.row_count = 0
        self.start_offset = out.tell()

    # -- appends ----------------------------------------------------------
    def append_token(self, col_id, tok):
        cs = self.cols[col_id]
        cs.struct_buf.append(tok)
        self._flush_full(cs, "struct")

    def append_value(self, col_id, leaf_kind, value):
        cs = self.cols[col_id]
        cs.struct_buf.append(TOK_VALUE)
        encode_value(cs.value_buf, leaf_kind, value)
        cs.value_count += 1
        self._flush_full(cs, "struct")
        self._flush_full(cs, "values")

    def _flush_full(self, cs, which):
        if which == "struct":
            buf, pages = cs.struct_buf, cs.struct_pages
            kind = layout.PAGE_STRUCT
        else:
            buf, pages = cs.value_buf, cs.value_pages
            kind = layout.PAGE_VALUES
        while len(buf) >= self.page_size:
            payload = bytes(buf[:self.page_size])
            del buf[:self.page_size]
            pages.append(_write_page_record(self.out, kind, len(pages), payload))
            if which == "struct":
                cs.flushed_struct += self.page_size
            else:
                cs.flushed_values += self.page_size

    # -- row boundaries -----------------------------------------------------
    def begin_row(self):
        for cs in self.cols.values():
            cs.row_struct_off.append(cs.struct_bytes)
            cs.row_value_off.append(cs.value_bytes)

    def end_row(self):
        self.row_count += 1

    # -- finish -------------------------------------------------------------
    def finish(self, rg_index):
        columns_meta = {}
        for col_id in sorted(self.cols):
            cs = self.cols[col_id]
            col = self.col_by_id[col_id]
            # flush remainders
            if cs.struct_buf:
                cs.struct_pages.append(
                    _write_page_record(self.out, layout.PAGE_STRUCT,
                                       len(cs.struct_pages), bytes(cs.struct_buf)))
                cs.flushed_struct += len(cs.struct_buf)
                cs.struct_buf = bytearray()
            if cs.value_buf:
                cs.value_pages.append(
                    _write_page_record(self.out, layout.PAGE_VALUES,
                                       len(cs.value_pages), bytes(cs.value_buf)))
                cs.flushed_values += len(cs.value_buf)
                cs.value_buf = bytearray()
            # row-index record
            blob = bytearray()
            put_uvarint(blob, self.row_count)
            prev_s = prev_v = 0
            for i in range(self.row_count):
                s, v = cs.row_struct_off[i], cs.row_value_off[i]
                put_uvarint(blob, s - prev_s)
                put_uvarint(blob, v - prev_v)
                prev_s, prev_v = s, v
            ri_off, ri_len = _write_page_record(
                self.out, layout.PAGE_ROW_INDEX, 0, bytes(blob))
            columns_meta[col.path] = {
                "id": col_id,
                "struct": {"bytes": cs.struct_bytes,
                           "pages": [list(p) for p in cs.struct_pages]},
                "values": {"bytes": cs.value_bytes, "count": cs.value_count,
                           "pages": [list(p) for p in cs.value_pages]},
                "row_index": [ri_off, ri_len],
            }
        meta = {"index": rg_index, "row_count": self.row_count,
                "columns": columns_meta}
        meta_json = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
        meta_offset = self.out.tell()
        self.out.write(struct.pack("<I", len(meta_json)))
        self.out.write(meta_json)
        self.out.write(struct.pack("<I", crc32(meta_json)))
        meta_len = 4 + len(meta_json) + 4
        return {"index": rg_index, "offset": self.start_offset,
                "row_count": self.row_count,
                "meta_offset": meta_offset, "meta_len": meta_len}


# ------------------------------------------------------------------ encoder

class _StructFrame:
    __slots__ = ("struct_def", "seen")

    def __init__(self, struct_def):
        self.struct_def = struct_def
        self.seen = set()


class _ListFrame:
    __slots__ = ("elem_type", "columns", "count")

    def __init__(self, elem_type, columns):
        self.elem_type = elem_type
        self.columns = columns        # columns receiving BEGIN/EMPTY/END
        self.count = 0


class _Encoder:
    """Schema-driven consumer of jsonsax events for one root row at a time."""

    def __init__(self, schema, rg):
        self.schema = schema
        self.rg = rg
        self.stack = []
        self.pending = None           # (Type, columns) awaiting a value
        self.row_no = 0
        self._root_type = _root_type_of(schema)

    def start_row(self, row_no):
        self.row_no = row_no
        self.stack = []
        self.pending = (self._root_type, [])
        self.rg.begin_row()

    def check_row_complete(self):
        if self.stack or self.pending is not None:
            raise DataError("row %d: truncated row" % self.row_no)
        self.rg.end_row()

    # -- helpers ----------------------------------------------------------
    def _err(self, msg):
        raise DataError("row %d: %s" % (self.row_no, msg))

    def _in_list(self):
        return bool(self.stack) and isinstance(self.stack[-1], _ListFrame)

    def _append(self, columns, tok):
        for col in columns:
            self.rg.append_token(col.id, tok)

    def _begin_list_elem(self, frame):
        if frame.count == 0:
            self._append(frame.columns, TOK_BEGIN)
        frame.count += 1

    def _take_context(self):
        """Returns (type_node, columns) for the value that is about to arrive."""
        if self._in_list():
            frame = self.stack[-1]
            self._begin_list_elem(frame)
            return frame.elem_type, frame.elem_type.columns
        if self.pending is None:
            self._err("internal: value without context")
        t, cols = self.pending
        self.pending = None
        return t, cols

    # -- events -------------------------------------------------------------
    def feed(self, ev):
        kind = ev[0]
        if kind == "start_object":
            self._start_object()
        elif kind == "key":
            self._key(ev[1])
        elif kind == "end_object":
            self._end_object()
        elif kind == "start_array":
            self._start_array()
        elif kind == "end_array":
            self._end_array()
        elif kind == "null":
            self._scalar_null()
        elif kind == "bool":
            self._scalar_value("bool", ev[1])
        elif kind == "number":
            self._scalar_number(ev[1])
        elif kind == "string":
            self._scalar_value("text", ev[1])
        else:  # pragma: no cover
            raise AssertionError(ev)

    def _start_object(self):
        t, cols = self._take_context()
        if t.kind != "struct":
            self._err("type mismatch: object where %s expected" % t.kind)
        self._append(cols, TOK_PRESENT)
        self.stack.append(_StructFrame(self.schema.structs[t.struct]))

    def _key(self, name):
        if not self.stack or not isinstance(self.stack[-1], _StructFrame):
            self._err("internal: key outside object")
        if self.pending is not None:
            self._err("internal: key while a value is pending")
        frame = self.stack[-1]
        f = frame.struct_def.by_name.get(name)
        if f is None:
            self._err("extra field %r not in struct %s"
                      % (name, frame.struct_def.name))
        if f.num in frame.seen:
            self._err("duplicate member %r in object" % name)
        frame.seen.add(f.num)
        self.pending = (f.type, f.columns)

    def _end_object(self):
        if not self.stack or not isinstance(self.stack[-1], _StructFrame):
            self._err("internal: object end outside object")
        if self.pending is not None:
            self._err("internal: object end while a value is pending")
        frame = self.stack.pop()
        for f in frame.struct_def.fields:
            if f.num not in frame.seen:
                if not f.type.nullable:
                    self._err("required field %r of struct %s is missing"
                              % (f.name, frame.struct_def.name))
                self._append(f.columns, TOK_MISS)

    def _start_array(self):
        t, cols = self._take_context()
        if t.kind != "list":
            self._err("type mismatch: array where %s expected" % t.kind)
        self.stack.append(_ListFrame(t.elem, cols))

    def _end_array(self):
        if not self._in_list():
            self._err("internal: array end outside array")
        frame = self.stack.pop()
        self._append(frame.columns, TOK_EMPTY if frame.count == 0 else TOK_END)

    def _scalar_null(self):
        t, cols = self._take_context()
        if not t.nullable:
            self._err("null for non-nullable %s" % t.kind)
        self._append(cols, TOK_NULL)

    def _scalar_value(self, ev_kind, value):
        t, cols = self._take_context()
        if t.kind not in LEAF_KINDS:
            self._err("type mismatch: scalar where %s expected" % t.kind)
        if t.kind != ev_kind:
            self._err("type mismatch: %s value for %s leaf" % (ev_kind, t.kind))
        for col in cols:
            self.rg.append_value(col.id, t.kind, value)

    def _scalar_number(self, raw):
        t, cols = self._take_context()
        if t.kind not in LEAF_KINDS:
            self._err("type mismatch: number where %s expected" % t.kind)
        if t.kind != "int64":
            self._err("type mismatch: number for %s leaf" % t.kind)
        # strict integer lexical form: -?(0|[1-9][0-9]*)
        body = raw[1:] if raw.startswith("-") else raw
        if not body or any(c not in _INT_RE_CHARS for c in body):
            self._err("non-integer number %r for int64 leaf" % raw)
        if len(body) > 1 and body.startswith("0"):
            self._err("leading zero in number %r" % raw)
        value = int(raw)
        if not (INT64_MIN <= value <= INT64_MAX):
            self._err("int64 out of range: %r" % raw)
        for col in cols:
            self.rg.append_value(col.id, "int64", value)


def _root_type_of(schema):
    from .schema import Type
    return Type("struct", False, struct=schema.root)


# ------------------------------------------------------------------ builder

DEFAULT_PAGE_SIZE = 65536
DEFAULT_ROWS_PER_GROUP = 100000
DEFAULT_MAX_SCALAR = 64 * 1024 * 1024


def build(schema, input_fileobj, out, page_size=DEFAULT_PAGE_SIZE,
          rows_per_group=DEFAULT_ROWS_PER_GROUP, max_scalar=DEFAULT_MAX_SCALAR):
    """Build an archive.  Returns the number of root rows written.

    ``schema`` is a Schema instance; ``input_fileobj`` a binary file object
    with JSONL content; ``out`` a binary file object positioned at 0.
    """
    if page_size < 4:
        raise DataError("page_size must be >= 4")
    if rows_per_group < 1:
        raise DataError("rows_per_group must be >= 1")

    columns = schema.columns
    header = {
        "format": layout.FORMAT_VERSION,
        "tool": "nestedcol/1",
        "schema": schema.raw,
        "columns": [{"id": c.id, "path": c.path, "leaf": c.leaf_kind}
                    for c in columns],
    }
    header_json = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    out.write(layout.MAGIC_H)
    out.write(struct.pack("<I", len(header_json)))
    out.write(header_json)
    out.write(struct.pack("<I", crc32(header_json)))

    rg_entries = []
    total_rows = 0
    rg = _RowGroupWriter(out, schema, page_size)
    encoder = _Encoder(schema, rg)

    for ev in jsonsax.parse_events(input_fileobj, max_scalar):
        kind = ev[0]
        if kind == "row_begin":
            encoder.start_row(total_rows)
            continue
        if kind == "row_end":
            encoder.check_row_complete()
            total_rows += 1
            if rg.row_count >= rows_per_group:
                rg_entries.append(rg.finish(len(rg_entries)))
                rg = _RowGroupWriter(out, schema, page_size)
                encoder = _Encoder(schema, rg)
            continue
        encoder.feed(ev)

    if rg.row_count > 0:
        rg_entries.append(rg.finish(len(rg_entries)))

    footer = {
        "format": layout.FORMAT_VERSION,
        "total_rows": total_rows,
        "row_groups": rg_entries,
        "schema_crc32": crc32(json.dumps(schema.raw, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")),
    }
    footer_json = json.dumps(footer, sort_keys=True, separators=(",", ":")).encode("utf-8")
    out.write(footer_json)
    out.write(struct.pack("<I", crc32(footer_json)))
    out.write(struct.pack("<I", len(footer_json)))
    out.write(layout.MAGIC_F)
    return total_rows
