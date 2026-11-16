"""scan-path: project one leaf column, optionally filtered by a predicate.

Predicate semantics (unit = root row):
  * a row matches iff at least one *actual leaf position* of the selected
    path satisfies the condition;
  * missing ancestors, null ancestors (null struct/list fields) and empty
    lists create NO positions and never satisfy anything;
  * a null list element reached while descending, and a null leaf value,
    are actual null positions and satisfy is_null;
  * eq matches an actual non-null leaf value equal to the constant.

For every matched row the COMPLETE projection of the path is emitted (all
elements, with missing/null/empty structure and original list indices), not
just the matching elements.  Rows are processed in source order.

A row is never materialised: the predicate is evaluated by a streaming walk
over the row's token/value ranges (pass 1), and the projection is emitted by
a second streaming walk over the same immutable, CRC-protected bytes
(pass 2).  The filter conclusion is a pure function of those bytes, so a row
spanning many pages always yields one consistent decision; output order is
the stream order, i.e. the original document order.
"""

import json

from .encoding import (TOK_BEGIN, TOK_EMPTY, TOK_END, TOK_MISS, TOK_NULL,
                       TOK_PRESENT, TOK_VALUE, decode_value, get_uvarint)
from .errors import PageCorrupt


class Predicate:
    def __init__(self, is_null=False, eq=None):
        self.is_null = is_null
        self.eq = eq

    @property
    def empty(self):
        return not self.is_null and self.eq is None

    @property
    def needs_values(self):
        return self.eq is not None


class _EarlyHit(Exception):
    pass


# ------------------------------------------------------------- level walker

def walk_column(levels, tr, vr, visitor):
    """Decode exactly one root row's structure for a column and drive the
    visitor.  ``tr`` is the struct-token reader, ``vr`` the value reader
    (may be None only if the visitor never asks for values).  The caller is
    responsible for checking that the readers are exactly exhausted."""
    t = get_uvarint(tr)
    _walk_token(levels, 0, t, tr, vr, visitor, [])


def _read_token(tr):
    return get_uvarint(tr)


def _walk_token(levels, i, t, tr, vr, vis, pos):
    lv = levels[i]
    if lv.kind == "field" and t == TOK_MISS:
        vis.missing(pos, lv)
        return
    if t == TOK_NULL:
        vis.null(pos, lv)
        return
    if lv.target == "leaf":
        if t != TOK_VALUE:
            raise PageCorrupt("bad token %d at leaf level %s" % (t, lv.label))
        if vr is None:
            # this pass does not read the value stream (e.g. is_null eval)
            vis.value_skipped(pos, lv)
        else:
            vis.value(pos, lv, decode_value(vr, lv.leaf_kind))
        return
    if lv.target == "struct":
        if t != TOK_PRESENT:
            raise PageCorrupt("bad token %d at struct level %s" % (t, lv.label))
        vis.struct_begin(pos, lv)
        _walk_token(levels, i + 1, _read_token(tr), tr, vr, vis, pos)
        vis.struct_end(pos, lv)
        return
    # list level
    if t == TOK_EMPTY:
        vis.empty(pos, lv)
        return
    if t != TOK_BEGIN:
        raise PageCorrupt("bad token %d at list level %s" % (t, lv.label))
    vis.list_begin(pos, lv)
    idx = 0
    while True:
        t2 = _read_token(tr)
        if t2 == TOK_END:
            break
        _walk_token(levels, i + 1, t2, tr, vr, vis, pos + [idx])
        idx += 1
    vis.list_end(pos, lv)


# ---------------------------------------------------------------- visitors

class _EvalVisitor:
    """Pass 1: decide whether the row matches.  O(schema depth) memory."""

    def __init__(self, pred):
        self.pred = pred
        self.hit = False

    def missing(self, pos, lv):
        pass

    def null(self, pos, lv):
        # an actual null position: null list element (any depth) or null leaf
        if self.pred.is_null and (lv.kind == "elem" or lv.target == "leaf"):
            self.hit = True
            raise _EarlyHit()

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
        if self.pred.eq is not None and v == self.pred.eq:
            self.hit = True
            raise _EarlyHit()

    def value_skipped(self, pos, lv):
        # a non-null leaf value; only relevant for eq, which always reads values
        pass


class _EventVisitor:
    """Pass 2 (events mode): emit one JSON event object per line."""

    def __init__(self, out, row_no):
        self.out = out
        self.row_no = row_no

    def _emit(self, obj):
        self.out.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def row_start(self, line_no):
        self._emit({"t": "row_start", "row": self.row_no, "line": line_no})

    def row_end(self):
        self._emit({"t": "row_end", "row": self.row_no})

    def missing(self, pos, lv):
        self._emit({"t": "missing", "at": lv.label, "pos": pos})

    def null(self, pos, lv):
        self._emit({"t": "null", "at": lv.label, "pos": pos})

    def empty(self, pos, lv):
        self._emit({"t": "empty", "at": lv.label, "pos": pos})

    def list_begin(self, pos, lv):
        self._emit({"t": "list_start", "at": lv.label, "pos": pos})

    def list_end(self, pos, lv):
        self._emit({"t": "list_end", "at": lv.label, "pos": pos})

    def struct_begin(self, pos, lv):
        self._emit({"t": "struct_start", "at": lv.label, "pos": pos})

    def struct_end(self, pos, lv):
        self._emit({"t": "struct_end", "at": lv.label, "pos": pos})

    def value(self, pos, lv, v):
        self._emit({"t": "value", "at": lv.label, "pos": pos, "value": v})

    def value_skipped(self, pos, lv):
        pass


class _AssembleVisitor:
    """Pass 2 (projection mode): write the row's projection as one JSON
    document, streaming -- no row is buffered.  Missing is rendered as
    {"$missing": true}; null as null; empty list as []."""

    def __init__(self, out, row_no):
        self.out = out
        self.row_no = row_no
        self.stack = []               # "list" or "struct"
        self.counts = []              # element count of each open list

    def row_start(self, line_no):
        self.out.write('{"row":%d,"line":%d,"projection":'
                       % (self.row_no, line_no))

    def row_end(self):
        self.out.write("}\n")

    def _pre(self):
        # comma separator only between elements of the innermost open list
        if self.stack and self.stack[-1] == "list":
            if self.counts[-1] > 0:
                self.out.write(",")
            self.counts[-1] += 1

    def missing(self, pos, lv):
        self._pre()
        self.out.write('{"$missing":true}')

    def null(self, pos, lv):
        self._pre()
        self.out.write("null")

    def empty(self, pos, lv):
        self._pre()
        self.out.write("[]")

    def list_begin(self, pos, lv):
        self._pre()
        self.out.write("[")
        self.stack.append("list")
        self.counts.append(0)

    def list_end(self, pos, lv):
        self.stack.pop()
        self.counts.pop()
        self.out.write("]")

    def struct_begin(self, pos, lv):
        # a struct on the path renders as {"<child_field>": <subtree>}
        self._pre()
        self.out.write("{")
        self.out.write(json.dumps(lv.child_field, ensure_ascii=False))
        self.out.write(":")
        self.stack.append("struct")

    def struct_end(self, pos, lv):
        self.stack.pop()
        self.out.write("}")

    def value(self, pos, lv, v):
        self._pre()
        self.out.write(json.dumps(v, ensure_ascii=False))

    def value_skipped(self, pos, lv):
        pass


# ------------------------------------------------------------------- driver

def scan_path(archive, path, predicate, out, assemble=False):
    """Run a scan-path over the archive.  Returns (rows_total, rows_matched).

    Only the selected column's struct/value pages are read (plus the root
    row-group index and the column's row index); every read is logged through
    the archive's page log.  On corruption an NcolError subclass propagates
    and no success marker is written.
    """
    col = archive.schema.column_by_path(path)
    rows_total = 0
    rows_matched = 0
    for rg_index in range(len(archive.row_groups)):
        meta = archive.rg_meta(rg_index)
        try:
            colmeta = meta["columns"][col.path]
        except KeyError:
            from .errors import IndexCorrupt
            raise IndexCorrupt("row group %d: column %s missing"
                               % (rg_index, col.path))
        s_offs, v_offs = archive.row_index(rg_index, col.path)
        struct_stream = archive.stream(rg_index, col.path, "struct")
        value_stream = archive.stream(rg_index, col.path, "values")
        n = meta["row_count"]
        for i in range(n):
            row_no = rows_total
            rows_total += 1
            s0 = s_offs[i]
            s1 = s_offs[i + 1] if i + 1 < n else colmeta["struct"]["bytes"]
            v0 = v_offs[i]
            v1 = v_offs[i + 1] if i + 1 < n else colmeta["values"]["bytes"]
            matched = True
            if not predicate.empty:
                tr = struct_stream.reader(s0, s1)
                vr = value_stream.reader(v0, v1) if predicate.needs_values else None
                vis = _EvalVisitor(predicate)
                try:
                    walk_column(col.levels, tr, vr, vis)
                except _EarlyHit:
                    pass
                matched = vis.hit
            if not matched:
                continue
            rows_matched += 1
            tr = struct_stream.reader(s0, s1)
            vr = value_stream.reader(v0, v1)
            vis = _AssembleVisitor(out, row_no) if assemble else _EventVisitor(out, row_no)
            vis.row_start(row_no + 1)
            walk_column(col.levels, tr, vr, vis)
            if not tr.at_end() or not vr.at_end():
                raise PageCorrupt("row %d did not consume its exact byte range"
                                  % row_no)
            vis.row_end()
    out.write(json.dumps({"t": "scan_end", "status": "ok",
                          "rows": rows_total, "matched": rows_matched}) + "\n")
    return rows_total, rows_matched
