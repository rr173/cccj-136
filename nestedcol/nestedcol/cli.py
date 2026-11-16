"""Command line interface.

  build       JSONL -> archive
  scan-path   project one leaf path, optionally filtered (--eq / --is-null)
  check-pages verify pages, boundaries and leaf counts
  project     independent row-wise projector (small inputs; reference output)
  dump-index  print the root row-group index and per-column page directories
  corrupt     test helper: flip a byte or truncate a file
"""

import argparse
import json
import resource
import sys

from . import __version__
from .builder import (DEFAULT_MAX_SCALAR, DEFAULT_PAGE_SIZE,
                      DEFAULT_ROWS_PER_GROUP, build)
from .checker import check_archive
from .errors import NcolError, SchemaError, UsageError
from .projector import matches as proj_matches, project_rows
from .reader import Archive
from .scanner import Predicate, scan_path
from .schema import Schema


def _load_schema(path):
    with open(path, "r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except ValueError as exc:
            raise SchemaError("schema file %s: invalid JSON: %s" % (path, exc))
    return Schema.from_dict(raw)


def _peak_rss_bytes():
    # Linux: ru_maxrss is in KiB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _print_memstats():
    sys.stderr.write("MEM peak_rss_bytes=%d\n" % _peak_rss_bytes())


def _open_page_log(spec):
    if spec == "-":
        return sys.stderr, lambda line: sys.stderr.write(line + "\n")
    f = open(spec, "w", encoding="utf-8")
    return f, lambda line: f.write(line + "\n")


# ------------------------------------------------------------------ commands

def cmd_build(args):
    schema = _load_schema(args.schema)
    with open(args.input, "rb") as inf, open(args.output, "wb") as outf:
        rows = build(schema, inf, outf, page_size=args.page_size,
                     rows_per_group=args.rows_per_group,
                     max_scalar=args.max_scalar)
    sys.stderr.write("built %s: %d row(s), page_size=%d, rows_per_group=%d\n"
                     % (args.output, rows, args.page_size, args.rows_per_group))
    if args.memstats:
        _print_memstats()
    return 0


def _parse_predicate(args, leaf_kind):
    if args.is_null and args.eq is not None:
        raise UsageError("--is-null and --eq are mutually exclusive")
    if args.is_null:
        return Predicate(is_null=True)
    if args.eq is not None:
        if leaf_kind == "int64":
            try:
                v = int(args.eq, 10)
            except ValueError:
                raise UsageError("--eq value %r is not an int64" % args.eq)
            from .encoding import INT64_MAX, INT64_MIN
            if not (INT64_MIN <= v <= INT64_MAX):
                raise UsageError("--eq value out of int64 range")
            return Predicate(eq=v)
        if leaf_kind == "bool":
            if args.eq not in ("true", "false"):
                raise UsageError("--eq value for bool must be true|false")
            return Predicate(eq=(args.eq == "true"))
        return Predicate(eq=args.eq)          # text
    return Predicate()


def cmd_scan_path(args):
    log_f, log = _open_page_log(args.page_log)
    try:
        with Archive(args.archive, log=log) as archive:
            pred = _parse_predicate(args, _leaf_kind_of(archive, args.path))
            out = sys.stdout
            scan_path(archive, args.path, pred, out, assemble=args.assemble)
            out.flush()
    finally:
        if log_f is not sys.stderr:
            log_f.close()
    if args.memstats:
        _print_memstats()
    return 0


def _leaf_kind_of(archive, path):
    return archive.schema.column_by_path(path).leaf_kind


def cmd_check_pages(args):
    log_f, log = _open_page_log(args.page_log)
    try:
        with Archive(args.archive, log=log) as archive:
            ok = check_archive(archive, sys.stdout)
    finally:
        if log_f is not sys.stderr:
            log_f.close()
    return 0 if ok else 4


def cmd_project(args):
    schema = _load_schema(args.schema)
    try:
        rows = project_rows(schema.raw, args.input, args.path)
    except KeyError as exc:
        raise SchemaError(str(exc))
    pred = _parse_predicate(args, _project_leaf_kind(schema, args.path))
    matched = 0
    for info in rows:
        if not proj_matches(info, is_null=pred.is_null, eq=pred.eq):
            continue
        matched += 1
        sys.stdout.write(json.dumps(
            {"row": info["row"], "line": info["line"],
             "projection": info["projection"]}, ensure_ascii=False) + "\n")
    sys.stdout.write(json.dumps({"t": "scan_end", "status": "ok",
                                 "rows": len(rows), "matched": matched}) + "\n")
    return 0


def _project_leaf_kind(schema, path):
    return schema.column_by_path(path).leaf_kind


def cmd_dump_index(args):
    with Archive(args.archive) as archive:
        out = {
            "format": archive.header["format"],
            "total_rows": archive.total_rows,
            "columns": archive.header["columns"],
            "row_groups": [],
        }
        for rg_index, rg in enumerate(archive.row_groups):
            meta = archive.rg_meta(rg_index)
            entry = {"index": rg["index"], "offset": rg["offset"],
                     "row_count": rg["row_count"],
                     "meta_offset": rg["meta_offset"], "meta_len": rg["meta_len"],
                     "columns": meta["columns"]}
            out["row_groups"].append(entry)
        json.dump(out, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    return 0


def cmd_corrupt(args):
    with open(args.input, "rb") as f:
        data = bytearray(f.read())
    if args.truncate is not None:
        data = data[:args.truncate]
    else:
        if not (0 <= args.offset < len(data)):
            raise UsageError("offset %d outside file of %d bytes"
                             % (args.offset, len(data)))
        data[args.offset] ^= args.xor
    with open(args.output, "wb") as f:
        f.write(data)
    sys.stderr.write("corrupted %s -> %s\n" % (args.input, args.output))
    return 0


# ------------------------------------------------------------------ argparse

def _parser():
    p = argparse.ArgumentParser(prog="nestedcol",
                                description="nested columnar page format tool")
    p.add_argument("--version", action="version",
                   version="nestedcol %s" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build an archive from JSONL")
    b.add_argument("--schema", required=True)
    b.add_argument("--input", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    b.add_argument("--rows-per-group", type=int, default=DEFAULT_ROWS_PER_GROUP)
    b.add_argument("--max-scalar", type=int, default=DEFAULT_MAX_SCALAR,
                   help="maximum size of one scalar (string/number), bytes")
    b.add_argument("--memstats", action="store_true")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("scan-path", help="project one leaf path")
    s.add_argument("archive")
    s.add_argument("path")
    s.add_argument("--eq", default=None, help="keep rows with a leaf value equal to this")
    s.add_argument("--is-null", action="store_true",
                   help="keep rows with at least one actual null leaf position")
    s.add_argument("--assemble", action="store_true",
                   help="emit one projection JSON per row instead of events")
    s.add_argument("--page-log", default="-", help="page read log file ('-' = stderr)")
    s.add_argument("--memstats", action="store_true")
    s.set_defaults(func=cmd_scan_path)

    c = sub.add_parser("check-pages", help="verify archive pages and boundaries")
    c.add_argument("archive")
    c.add_argument("--page-log", default="-")
    c.set_defaults(func=cmd_check_pages)

    j = sub.add_parser("project", help="independent row-wise projector (reference)")
    j.add_argument("--schema", required=True)
    j.add_argument("--input", required=True)
    j.add_argument("--path", required=True)
    j.add_argument("--eq", default=None)
    j.add_argument("--is-null", action="store_true")
    j.set_defaults(func=cmd_project)

    d = sub.add_parser("dump-index", help="print row-group index and page directories")
    d.add_argument("archive")
    d.set_defaults(func=cmd_dump_index)

    x = sub.add_parser("corrupt", help="test helper: flip a byte or truncate")
    x.add_argument("--input", required=True)
    x.add_argument("--output", required=True)
    x.add_argument("--offset", type=int, default=None)
    x.add_argument("--xor", type=lambda s: int(s, 0), default=0xFF)
    x.add_argument("--truncate", type=int, default=None)
    x.set_defaults(func=cmd_corrupt)
    return p


def main(argv=None):
    args = _parser().parse_args(argv)
    if getattr(args, "cmd", None) == "corrupt" and args.truncate is None and args.offset is None:
        sys.stderr.write("error: corrupt needs --offset or --truncate\n")
        return 2
    try:
        return args.func(args)
    except NcolError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return exc.exit_code
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
