"""Independent row-wise projector -- a deliberately simple, in-memory
reference implementation used to cross-check scan-path on small examples.

It shares NO code with the column format (no tokens, no levels, no streams):
each JSONL line is parsed with the stock json module and the path is walked
directly on the Python object tree.  Do not use it on large rows.
"""

import json

MISSING = {"$missing": True}


def _resolve(schema_dict, path):
    """Resolve a dotted path to a list of steps.  Each step is
    ("field", name, type_node, nullable) or ("elem", type_node, nullable).
    Lists are transparent in the path.  Returns (steps, leaf_kind)."""
    structs = schema_dict["structs"]
    cur_struct = schema_dict["root"]
    segments = path.split(".")
    steps = []
    for si, seg in enumerate(segments):
        fields = {f["name"]: f for f in structs[cur_struct]["fields"]}
        if seg not in fields:
            raise KeyError("no field %r in struct %s" % (seg, cur_struct))
        t = fields[seg]["type"]
        steps.append(("field", seg, t))
        # skip list levels transparently
        while t["kind"] == "list":
            t = t["elem"]
            steps.append(("elem", None, t))
        if t["kind"] == "struct":
            cur_struct = t["struct"]
        elif si != len(segments) - 1:
            raise KeyError("path %r continues past a leaf" % path)
    # after the last segment the type must be a leaf
    last_t = steps[-1][2]
    t = last_t
    while t["kind"] == "list":
        raise KeyError("path %r ends at a list, not a leaf" % path)
    if t["kind"] == "struct":
        raise KeyError("path %r ends at a struct, not a leaf" % path)
    return steps, t["kind"]


def _walk(steps, i, value, null_positions, values):
    """Project `value` at step i.  `value` is the (non-missing, non-null)
    JSON value of the enclosing slot."""
    kind, name, t = steps[i]
    if kind == "field":
        # value is the containing object
        if name not in value:
            return dict(MISSING)
        v = value[name]
        if v is None:
            if t["kind"] in ("int64", "text", "bool"):
                null_positions[0] += 1          # null leaf
            return None
        return _walk_type(steps, t, v, i, null_positions, values)
    else:  # elem
        if value is None:
            null_positions[0] += 1              # null list element
            return None
        return _walk_type(steps, t, value, i, null_positions, values)


def _walk_type(steps, t, v, i, null_positions, values):
    if t["kind"] == "list":
        return [_walk(steps, i + 1, e, null_positions, values) for e in v]
    if t["kind"] == "struct":
        # a struct on the path projects to {"<next field>": <subtree>} so
        # that a null element and a present-element-with-null-field differ
        nxt = steps[i + 1]
        return {nxt[1]: _walk(steps, i + 1, v, null_positions, values)}
    # leaf
    values.append(v)
    return v


def project_rows(schema_dict, jsonl_path, path):
    """Returns a list of dicts: {"row", "line", "projection",
    "null_positions", "values"} for each root row."""
    steps, leaf_kind = _resolve(schema_dict, path)
    out = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("line %d: root row must be an object" % line_no)
            null_positions = [0]
            values = []
            proj = _walk(steps, 0, obj, null_positions, values)
            out.append({"row": len(out), "line": line_no,
                        "projection": proj,
                        "null_positions": null_positions[0],
                        "values": values})
    return out


def matches(row_info, is_null=False, eq=None):
    if is_null:
        return row_info["null_positions"] > 0
    if eq is not None:
        return any(v == eq for v in row_info["values"])
    return True
