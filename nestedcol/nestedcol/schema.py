"""Schema model.

A schema defines named structs.  Every field has a number that is unique
within its owning struct and a name that is unique within its owning struct;
fields are identified by (owning struct, number) -- two structs may reuse the
same names/numbers without any relation.

Type nodes (JSON form):
  {"kind": "int64"|"text"|"bool", "nullable": bool}          leaf
  {"kind": "struct", "struct": "<name>", "nullable": bool}   struct reference
  {"kind": "list", "nullable": bool, "elem": <type node>}    list

Nullability rules (enforced by the builder):
  * a nullable struct field may be missing, explicitly null, or present;
    missing and null are distinct states.
  * a non-nullable field must be present and non-null.
  * list elements carry their own nullable flag on the elem type node.

From the schema we derive the flat list of *leaf columns* (one per reachable
leaf).  Each column owns two physical streams; see FORMAT.md.
"""

from .errors import SchemaError

LEAF_KINDS = ("int64", "text", "bool")


class Type:
    __slots__ = ("kind", "nullable", "struct", "elem", "columns")

    def __init__(self, kind, nullable, struct=None, elem=None):
        self.kind = kind            # "int64" | "text" | "bool" | "struct" | "list"
        self.nullable = nullable
        self.struct = struct        # struct name when kind == "struct"
        self.elem = elem            # elem Type when kind == "list"
        self.columns = []           # leaf columns whose path crosses this node


class Field:
    __slots__ = ("num", "name", "type", "columns")

    def __init__(self, num, name, type_):
        self.num = num
        self.name = name
        self.type = type_
        self.columns = []           # leaf columns whose path crosses this field


class StructDef:
    __slots__ = ("name", "fields", "by_name", "by_num")

    def __init__(self, name, fields):
        self.name = name
        self.fields = fields
        self.by_name = {f.name: f for f in fields}
        self.by_num = {f.num: f for f in fields}


class Level:
    """One nesting level of a column path (see FORMAT.md section 6).

    kind:   "field" (struct field; may carry MISS) or "elem" (list element)
    target: "struct" | "list" | "leaf" -- what follows when the level is present
    label:  human-readable path of this level, e.g. "items", "items[]", "items.x"
    """

    __slots__ = ("kind", "nullable", "target", "label", "field_name",
                 "leaf_kind", "child_field")

    def __init__(self, kind, nullable, target, label, field_name=None,
                 leaf_kind=None, child_field=None):
        self.kind = kind
        self.nullable = nullable
        self.target = target
        self.label = label
        self.field_name = field_name
        self.leaf_kind = leaf_kind
        self.child_field = child_field


class Column:
    __slots__ = ("id", "path", "leaf_kind", "levels")

    def __init__(self, col_id, path, leaf_kind, levels):
        self.id = col_id
        self.path = path
        self.leaf_kind = leaf_kind
        self.levels = levels


def _clone_levels(levels):
    """Deep-copy level objects: schema nodes are shared between columns
    (e.g. matrix.v and matrix.tag cross the same struct element), but each
    column needs its own Level objects (child_field differs)."""
    return [Level(lv.kind, lv.nullable, lv.target, lv.label, lv.field_name,
                  lv.leaf_kind, lv.child_field) for lv in levels]


def _parse_type(node, where):
    if not isinstance(node, dict):
        raise SchemaError("%s: type must be an object" % where)
    kind = node.get("kind")
    nullable = node.get("nullable")
    if not isinstance(nullable, bool):
        raise SchemaError("%s: 'nullable' must be a boolean" % where)
    if kind in LEAF_KINDS:
        return Type(kind, nullable)
    if kind == "struct":
        name = node.get("struct")
        if not isinstance(name, str) or not name:
            raise SchemaError("%s: struct type needs a 'struct' name" % where)
        return Type("struct", nullable, struct=name)
    if kind == "list":
        elem = node.get("elem")
        if elem is None:
            raise SchemaError("%s: list type needs 'elem'" % where)
        return Type("list", nullable, elem=_parse_type(elem, where + ".elem"))
    raise SchemaError("%s: unknown kind %r" % (where, kind))


class Schema:
    def __init__(self, root, structs, raw):
        self.root = root
        self.structs = structs          # name -> StructDef
        self.raw = raw                  # original dict (stored in the archive)
        self._columns = []
        self._by_path = {}

    # ------------------------------------------------------------ loading
    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise SchemaError("schema must be a JSON object")
        if d.get("format") != 1:
            raise SchemaError("unsupported schema format %r" % (d.get("format"),))
        root = d.get("root")
        structs_d = d.get("structs")
        if not isinstance(root, str) or not isinstance(structs_d, dict) or not structs_d:
            raise SchemaError("schema needs 'root' and a non-empty 'structs' object")
        structs = {}
        for name, sd in structs_d.items():
            if not isinstance(name, str) or not name:
                raise SchemaError("struct names must be non-empty strings")
            if not isinstance(sd, dict) or not isinstance(sd.get("fields"), list):
                raise SchemaError("struct %s: needs a 'fields' list" % name)
            fields = []
            seen_nums, seen_names = set(), set()
            for fd in sd["fields"]:
                where = "struct %s field %r" % (name, fd.get("name") if isinstance(fd, dict) else fd)
                if not isinstance(fd, dict):
                    raise SchemaError("%s: field must be an object" % where)
                num, fname = fd.get("num"), fd.get("name")
                if not isinstance(num, int) or isinstance(num, bool) or num <= 0:
                    raise SchemaError("%s: 'num' must be a positive integer" % where)
                if not isinstance(fname, str) or not fname:
                    raise SchemaError("%s: 'name' must be a non-empty string" % where)
                if num in seen_nums:
                    raise SchemaError("struct %s: duplicate field number %d" % (name, num))
                if fname in seen_names:
                    raise SchemaError("struct %s: duplicate field name %r" % (name, fname))
                seen_nums.add(num)
                seen_names.add(fname)
                fields.append(Field(num, fname, _parse_type(fd.get("type"), where)))
            if not fields:
                raise SchemaError("struct %s: must have at least one field" % name)
            structs[name] = StructDef(name, fields)
        if root not in structs:
            raise SchemaError("root struct %r not defined" % root)
        schema = cls(root, structs, d)
        schema._check_refs_and_cycles()
        schema._build_columns()
        return schema

    def _check_refs_and_cycles(self):
        visiting, done = set(), set()

        def walk_type(t):
            if t.kind == "struct":
                if t.struct not in self.structs:
                    raise SchemaError("unknown struct %r" % t.struct)
                walk_struct(t.struct)
            elif t.kind == "list":
                walk_type(t.elem)

        def walk_struct(name):
            if name in done:
                return
            if name in visiting:
                raise SchemaError("struct cycle through %r" % name)
            visiting.add(name)
            for f in self.structs[name].fields:
                walk_type(f.type)
            visiting.discard(name)
            done.add(name)

        walk_struct(self.root)

    # ------------------------------------------------------------- columns
    def _build_columns(self):
        """Enumerate leaf columns and register each column on every Field /
        list-elem Type node it crosses (the builder appends tokens per node)."""
        columns = []

        def walk_fields(struct_name, path, levels, nodes):
            for f in self.structs[struct_name].fields:
                fpath = f.name if not path else path + "." + f.name
                lvl = Level("field", f.type.nullable, None, fpath,
                            field_name=f.name)
                walk_type(f.type, fpath, levels + [lvl], nodes + [f])

        def walk_type(t, label, levels, nodes):
            lvl = levels[-1]
            if t.kind == "list":
                lvl.target = "list"
                walk_elem(t.elem, label, levels, nodes + [t.elem])
            elif t.kind == "struct":
                lvl.target = "struct"
                walk_fields(t.struct, label, levels, nodes)
            else:
                lvl.target = "leaf"
                lvl.leaf_kind = t.kind
                col = Column(len(columns), label, t.kind, _clone_levels(levels))
                columns.append(col)
                for node in nodes:
                    node.columns.append(col)

        def walk_elem(elem_t, label, levels, nodes):
            elvl = Level("elem", elem_t.nullable, None, label + "[]")
            walk_elem_type(elem_t, label, levels + [elvl], nodes)

        def walk_elem_type(elem_t, label, levels, nodes):
            elvl = levels[-1]
            if elem_t.kind == "list":
                elvl.target = "list"
                walk_elem(elem_t.elem, label, levels, nodes + [elem_t.elem])
            elif elem_t.kind == "struct":
                elvl.target = "struct"
                walk_fields(elem_t.struct, label, levels, nodes)
            else:
                elvl.target = "leaf"
                elvl.leaf_kind = elem_t.kind
                col = Column(len(columns), label, elem_t.kind,
                             _clone_levels(levels))
                columns.append(col)
                for node in nodes:
                    node.columns.append(col)

        walk_fields(self.root, "", [], [])

        columns.sort(key=lambda c: c.path)
        for new_id, col in enumerate(columns):
            col.id = new_id
            for i, lv in enumerate(col.levels):
                if lv.target == "struct":
                    lv.child_field = col.levels[i + 1].field_name
        self._columns = columns
        self._by_path = {c.path: c for c in columns}

    # ------------------------------------------------------------ accessors
    @property
    def columns(self):
        return list(self._columns)

    def column_by_path(self, path):
        col = self._by_path.get(path)
        if col is None:
            raise SchemaError(
                "no leaf column at path %r (leaf columns: %s)"
                % (path, ", ".join(sorted(self._by_path))))
        return col

    def leaf_paths(self):
        return sorted(self._by_path)
