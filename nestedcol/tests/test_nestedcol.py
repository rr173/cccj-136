"""End-to-end and unit tests for nestedcol.  Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENT_SCHEMA = os.path.join(ROOT, "schemas", "event.schema.json")
STRICT_SCHEMA = os.path.join(ROOT, "schemas", "strict.schema.json")
FIXTURES = os.path.join(ROOT, "examples", "make_fixtures.py")

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def run_cli(*args, input_bytes=None):
    """Runs the CLI; returns (exit_code, stdout, stderr)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    p = subprocess.run([sys.executable, "-m", "nestedcol"] + list(args),
                       capture_output=True, text=True, cwd=ROOT, env=env)
    return p.returncode, p.stdout, p.stderr


def json_lines(text):
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def make_fixtures(outdir, big=False, big_items=1_000_000, big_payload=5_000_000):
    args = [sys.executable, FIXTURES, outdir]
    if big:
        args.append("big")
    env = dict(os.environ)
    subprocess.run(args, check=True, cwd=ROOT, env=env,
                   capture_output=True, text=True)


def build(schema, inp, out, page_size=1024, rows_per_group=100, max_scalar=None):
    args = ["build", "--schema", schema, "--input", inp, "--output", out,
            "--page-size", str(page_size), "--rows-per-group", str(rows_per_group)]
    if max_scalar is not None:
        args += ["--max-scalar", str(max_scalar)]
    return run_cli(*args)


def scan(archive, path, *extra, page_log=None):
    args = ["scan-path", archive, path, "--page-log", page_log or os.devnull]
    args += list(extra)
    return run_cli(*args)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ncol_test_")
        make_fixtures(cls.tmp)
        cls.events_jsonl = os.path.join(cls.tmp, "events.jsonl")
        cls.edge_jsonl = os.path.join(cls.tmp, "edge.jsonl")
        cls.events = os.path.join(cls.tmp, "events.ncol")
        rc, _, err = build(EVENT_SCHEMA, cls.events_jsonl, cls.events,
                           page_size=1024, rows_per_group=100)
        assert rc == 0, err


# ------------------------------------------------------------------ fixtures

class TestFiveFixedRows(Base):
    """The required five-row semantics, on the fixture with exactly the
    five fixed rows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.five = os.path.join(cls.tmp, "five.ncol")
        rc, _, err = build(EVENT_SCHEMA, os.path.join(cls.tmp, "five.jsonl"),
                           cls.five, page_size=1024, rows_per_group=100)
        assert rc == 0, err

    def test_is_null_selects_exactly_rows_4_and_5(self):
        rc, out, err = scan(self.five, "items.x", "--is-null", "--assemble")
        self.assertEqual(rc, 0, err)
        rows = json_lines(out)
        proj = {r["row"]: r["projection"] for r in rows if "projection" in r}
        self.assertEqual(sorted(proj), [3, 4])                # 0-based: lines 4, 5
        self.assertEqual(proj[3], [None])                     # items=[null]
        self.assertEqual(proj[4], [{"x": None}, {"x": 7}, {"x": 7}])
        end = rows[-1]
        self.assertEqual((end["t"], end["status"], end["matched"]),
                         ("scan_end", "ok", 2))

    def test_unconditional_distinguishes_first_four(self):
        rc, out, err = scan(self.five, "items.x", "--assemble")
        self.assertEqual(rc, 0, err)
        rows = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(rows), 5)
        p = [r["projection"] for r in rows[:4]]
        self.assertEqual(p[0], {"$missing": True})   # missing items
        self.assertEqual(p[1], None)                 # items=null
        self.assertEqual(p[2], [])                   # items=[]
        self.assertEqual(p[3], [None])               # items=[null]
        self.assertEqual(len({json.dumps(x, sort_keys=True) for x in p}), 4)

    def test_row5_indices_preserved_in_events(self):
        rc, out, err = scan(self.five, "items.x", "--eq", "7")
        self.assertEqual(rc, 0, err)
        events = json_lines(out)
        row5 = []
        in_row = False
        for e in events:
            if e["t"] == "row_start" and e["row"] == 4:
                in_row = True
            elif e["t"] == "row_end" and in_row:
                break
            elif in_row and e["t"] in ("null", "value"):
                row5.append((e["t"], tuple(e["pos"]), e.get("value")))
        self.assertEqual(row5, [("null", (0,), None),
                                ("value", (1,), 7),
                                ("value", (2,), 7)])
        # row numbers and line numbers present
        starts = [e for e in events if e["t"] == "row_start"]
        self.assertEqual(starts[0]["line"], starts[0]["row"] + 1)

    def test_row5_large_payload_present(self):
        # the payload of row 5 is stored in its own column and projects fully
        rc, out, err = scan(self.five, "payload", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = {r["row"]: r["projection"] for r in json_lines(out)
                if "projection" in r}
        self.assertEqual(proj[4], "PAYLOAD-" * 7500)
        self.assertEqual(proj[0], {"$missing": True})


class TestNestedAndPredicates(Base):
    def test_matrix_v_is_null(self):
        rc, out, err = scan(self.events, "matrix.v", "--is-null", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = {r["row"]: r["projection"] for r in json_lines(out)
                if "projection" in r}
        self.assertEqual(sorted(proj), [5, 6, 7])
        self.assertEqual(proj[5], [[None], []])
        self.assertEqual(proj[6], [None])
        self.assertEqual(proj[7], [[{"v": -5}], [None, {"v": 9}]])

    def test_matrix_v_eq_negative(self):
        rc, out, err = scan(self.events, "matrix.v", "--eq", "-5", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["row"], 7)

    def test_meta_flag_bool_leaf(self):
        rc, out, err = scan(self.events, "meta.flag", "--eq", "true", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["projection"], {"flag": True})
        rc, out, err = scan(self.events, "meta.flag", "--is-null", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["projection"], {"flag": None})

    def test_eq_int64_min_max(self):
        rc, out, _ = scan(self.events, "items.x", "--eq", str(INT64_MIN), "--assemble")
        self.assertEqual(rc, 0)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["projection"], [{"x": INT64_MIN}])
        rc, out, _ = scan(self.events, "items.x", "--eq", str(INT64_MAX), "--assemble")
        self.assertEqual(rc, 0)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["projection"], [{"x": INT64_MAX}])

    def test_eq_text_and_long_text(self):
        rc, out, _ = scan(self.events, "payload", "--eq", "p6", "--assemble")
        self.assertEqual(rc, 0)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["row"], 5)
        # long unicode text round-trips exactly
        src_rows = [json.loads(l) for l in open(self.events_jsonl, encoding="utf-8")]
        long_text = src_rows[9]["payload"]
        rc, out, _ = scan(self.events, "payload", "--assemble")
        self.assertEqual(rc, 0)
        proj = {r["row"]: r["projection"] for r in json_lines(out)
                if "projection" in r}
        self.assertEqual(proj[9], long_text)
        self.assertGreater(len(long_text), 5000)

    def test_unknown_path_rejected(self):
        rc, out, err = scan(self.events, "items.zz")
        self.assertEqual(rc, 5)
        rc, out, err = scan(self.events, "items")     # not a leaf path
        self.assertEqual(rc, 5)


class TestProjectorAgreement(Base):
    def test_all_paths_agree_with_independent_projector(self):
        for path in ("items.x", "matrix.v", "matrix.tag", "meta.flag", "payload"):
            rc, out, err = scan(self.events, path, "--assemble")
            self.assertEqual(rc, 0, err)
            got = [r for r in json_lines(out) if "projection" in r]
            rc, out, err = run_cli("project", "--schema", EVENT_SCHEMA,
                                   "--input", self.events_jsonl, "--path", path)
            self.assertEqual(rc, 0, err)
            want = [r for r in json_lines(out) if "projection" in r]
            self.assertEqual(got, want, "path %s" % path)

    def test_projector_predicates_agree(self):
        for path, extra in (("items.x", ["--is-null"]),
                            ("matrix.v", ["--eq", "-5"]),
                            ("meta.flag", ["--eq", "true"])):
            rc, out, _ = scan(self.events, path, *(extra + ["--assemble"]))
            got = [r["row"] for r in json_lines(out) if "projection" in r]
            rc, out, _ = run_cli("project", "--schema", EVENT_SCHEMA,
                                 "--input", self.events_jsonl, "--path", path,
                                 *extra)
            want = [r["row"] for r in json_lines(out) if "projection" in r]
            self.assertEqual(got, want)


class TestChecker(Base):
    def test_checker_ok(self):
        rc, out, err = run_cli("check-pages", self.events, "--page-log", os.devnull)
        self.assertEqual(rc, 0, err)
        self.assertIn("CHECK OK", out)
        self.assertIn("leaves=", out)   # actual leaf counts reported

    def test_checker_detects_corruption(self):
        bad = os.path.join(self.tmp, "events_bad.ncol")
        rc, out, _ = run_cli("dump-index", self.events)
        idx = json.loads(out)
        off = idx["row_groups"][0]["columns"]["items.x"]["struct"]["pages"][0][0]
        run_cli("corrupt", "--input", self.events, "--output", bad,
                "--offset", str(off + 15))
        rc, out, err = run_cli("check-pages", bad, "--page-log", os.devnull)
        self.assertNotEqual(rc, 0)
        self.assertIn("CHECK FAIL", out)


class TestRowGroupsAndBoundaries(Base):
    def test_multiple_row_groups_global_row_numbers(self):
        arc = os.path.join(self.tmp, "events_rg2.ncol")
        rc, _, err = build(EVENT_SCHEMA, self.events_jsonl, arc,
                           page_size=256, rows_per_group=2)
        self.assertEqual(rc, 0, err)
        rc, out, _ = run_cli("dump-index", arc)
        idx = json.loads(out)
        self.assertEqual(len(idx["row_groups"]), 8)     # 15 rows / 2 per group
        rc, out, err = scan(arc, "items.x", "--assemble")
        self.assertEqual(rc, 0, err)
        rows = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual([r["row"] for r in rows], list(range(15)))
        self.assertEqual(rows[4]["projection"], [{"x": None}, {"x": 7}, {"x": 7}])

    def test_row_boundary_at_page_edge(self):
        # 40 rows {"items":[]}: items.x struct stream = 1 byte/row, so with
        # page-size 8 every 8th row boundary lands exactly on a page edge
        arc = os.path.join(self.tmp, "edge.ncol")
        rc, _, err = build(EVENT_SCHEMA, self.edge_jsonl, arc,
                           page_size=8, rows_per_group=1000)
        self.assertEqual(rc, 0, err)
        # white-box: verify via the row index that row 8 starts at offset 8
        sys.path.insert(0, ROOT)
        from nestedcol.reader import Archive
        with Archive(arc) as a:
            s_offs, v_offs = a.row_index(0, "items.x")
            self.assertEqual(s_offs[8] % 8, 0)
            self.assertEqual(s_offs[8], 8)
            self.assertEqual(v_offs[0], 0)      # no values at all
        rc, out, err = scan(arc, "items.x", "--assemble")
        self.assertEqual(rc, 0, err)
        rows = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(r["projection"] == [] for r in rows))

    def test_stream_reader_exact_edge_ranges(self):
        sys.path.insert(0, ROOT)
        from nestedcol.reader import Archive
        arc = os.path.join(self.tmp, "edge.ncol")
        reads = []
        with Archive(arc, log=reads.append) as a:
            stream = a.stream(0, "items.x", "struct")
            # range ending exactly at a page edge must not touch the next page
            r = stream.reader(0, 8)
            self.assertEqual(r.read(8), b"\x03" * 8)   # TOK_EMPTY x 8
            self.assertEqual(len([l for l in reads if " PAGE " in l]), 1)
            # range starting exactly at a page edge
            reads.clear()
            r = stream.reader(8, 9)
            self.assertEqual(r.read(1), b"\x03")
            self.assertEqual(len([l for l in reads if " PAGE " in l]), 1)
            # empty range reads nothing
            reads.clear()
            r = stream.reader(8, 8)
            self.assertEqual(r.read(0), b"")
            self.assertEqual(len([l for l in reads if " PAGE " in l]), 0)


class TestEmptyAndTruncated(Base):
    def test_empty_input_builds_valid_empty_archive(self):
        empty_jsonl = os.path.join(self.tmp, "empty.jsonl")
        open(empty_jsonl, "w").close()
        arc = os.path.join(self.tmp, "empty.ncol")
        rc, _, err = build(EVENT_SCHEMA, empty_jsonl, arc)
        self.assertEqual(rc, 0, err)
        rc, out, err = run_cli("check-pages", arc, "--page-log", os.devnull)
        self.assertEqual(rc, 0, err)
        self.assertIn("CHECK OK", out)
        rc, out, err = scan(arc, "items.x", "--assemble")
        self.assertEqual(rc, 0, err)
        end = json_lines(out)[-1]
        self.assertEqual(end["rows"], 0)
        self.assertEqual(end["matched"], 0)
        rc, out, err = scan(arc, "items.x", "--is-null", "--assemble")
        self.assertEqual(rc, 0, err)

    def test_empty_file_is_not_an_archive(self):
        empty = os.path.join(self.tmp, "zero.ncol")
        open(empty, "w").close()
        rc, out, err = scan(empty, "items.x")
        self.assertEqual(rc, 3)
        self.assertNotIn('"status": "ok"', out)

    def test_truncated_archive_rejected(self):
        size = os.path.getsize(self.events)
        for cut in (5, 100, size // 2, size - 20, size - 3):
            bad = os.path.join(self.tmp, "trunc_%d.ncol" % cut)
            rc, _, _ = run_cli("corrupt", "--input", self.events,
                               "--output", bad, "--truncate", str(cut))
            self.assertEqual(rc, 0)
            rc, out, err = scan(bad, "items.x", "--assemble")
            self.assertNotEqual(rc, 0, "cut=%d" % cut)
            self.assertNotIn('"status": "ok"', out)
            rc, out, err = run_cli("check-pages", bad, "--page-log", os.devnull)
            self.assertNotEqual(rc, 0, "cut=%d" % cut)


class TestCorruptionIsolation(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        rc, out, _ = run_cli("dump-index", cls.events)
        cls.idx = json.loads(out)
        cls.cols = cls.idx["row_groups"][0]["columns"]

    def _corrupt_at(self, off, name="bad.ncol"):
        bad = os.path.join(self.tmp, name)
        rc, _, err = run_cli("corrupt", "--input", self.events,
                             "--output", bad, "--offset", str(off))
        self.assertEqual(rc, 0, err)
        return bad

    def test_corrupt_unselected_payload_page_narrow_scan_ok(self):
        # payload value page, inside its payload area
        off = self.cols["payload"]["values"]["pages"][0][0] + 20
        bad = self._corrupt_at(off, "bad_payload.ncol")
        log = os.path.join(self.tmp, "narrow.log")
        rc, out, err = scan(bad, "items.x", "--is-null", "--assemble", page_log=log)
        self.assertEqual(rc, 0, err)
        self.assertIn('"status": "ok"', out)
        proj = {r["row"]: r["projection"] for r in json_lines(out)
                if "projection" in r}
        self.assertEqual(proj[4], [{"x": None}, {"x": 7}, {"x": 7}])
        # the scan never touched a payload page
        logtxt = open(log).read()
        self.assertNotIn("col=payload", logtxt)
        self.assertIn("col=items.x", logtxt)
        # but the checker (which reads everything) detects the corruption
        rc, out, _ = run_cli("check-pages", bad, "--page-log", os.devnull)
        self.assertNotEqual(rc, 0)

    def test_corrupt_selected_struct_page_fails_no_success_marker(self):
        off = self.cols["items.x"]["struct"]["pages"][0][0] + 16
        bad = self._corrupt_at(off, "bad_struct.ncol")
        rc, out, err = scan(bad, "items.x", "--assemble")
        self.assertEqual(rc, 4)
        self.assertNotIn('"status": "ok"', out)

    def test_corrupt_selected_value_page_fails(self):
        off = self.cols["items.x"]["values"]["pages"][0][0] + 14
        bad = self._corrupt_at(off, "bad_value.ncol")
        rc, out, err = scan(bad, "items.x", "--eq", "7", "--assemble")
        self.assertEqual(rc, 4)
        self.assertNotIn('"status": "ok"', out)

    def test_corrupt_row_index_rejected(self):
        off = self.cols["items.x"]["row_index"][0] + 14
        bad = self._corrupt_at(off, "bad_rowindex.ncol")
        rc, out, err = scan(bad, "items.x", "--assemble")
        self.assertEqual(rc, 3)
        self.assertNotIn('"status": "ok"', out)

    def test_corrupt_footer_rejected(self):
        size = os.path.getsize(self.events)
        import struct as st
        with open(self.events, "rb") as f:
            f.seek(size - 12)
            (flen,) = st.unpack("<I", f.read(4))
        bad = self._corrupt_at(size - 16 - flen + 3, "bad_footer.ncol")
        rc, out, err = scan(bad, "items.x")
        self.assertEqual(rc, 3)
        rc, out, err = scan(self._corrupt_at(size - 2, "bad_magic.ncol"), "items.x")
        self.assertEqual(rc, 3)

    def test_corrupt_header_rejected(self):
        bad = self._corrupt_at(20, "bad_header.ncol")
        rc, out, err = scan(bad, "items.x")
        self.assertEqual(rc, 3)


class TestBuildRejections(Base):
    def _reject(self, text, schema=EVENT_SCHEMA):
        inp = os.path.join(self.tmp, "rej.jsonl")
        with open(inp, "w", encoding="utf-8") as f:
            f.write(text)
        rc, out, err = build(schema, inp, os.path.join(self.tmp, "rej.ncol"))
        self.assertEqual(rc, 6, "input %r should be rejected: %s" % (text, err))
        return err

    def test_extra_field(self):
        self.assertIn("extra field", self._reject('{"zz":1}\n'))

    def test_duplicate_member(self):
        self.assertIn("duplicate", self._reject('{"items":[],"items":[]}\n'))

    def test_string_for_int(self):
        self._reject('{"items":[{"x":"7"}]}\n')

    def test_float_for_int(self):
        self._reject('{"items":[{"x":7.0}]}\n')

    def test_bool_for_int(self):
        self._reject('{"items":[{"x":true}]}\n')

    def test_int_for_text(self):
        self._reject('{"payload":7}\n')

    def test_int64_overflow(self):
        self._reject('{"items":[{"x":9223372036854775808}]}\n')
        self._reject('{"items":[{"x":-9223372036854775809}]}\n')

    def test_leading_zero(self):
        self._reject('{"items":[{"x":01}]}\n')

    def test_root_not_object(self):
        self._reject('[1,2]\n')
        self._reject('7\n')
        self._reject('"hi"\n')

    def test_trailing_garbage(self):
        self._reject('{} garbage\n')
        self._reject('{} ,\n')

    def test_malformed_json(self):
        self._reject('{"items":\n')
        self._reject('{"items":[}\n')

    def test_wrong_nesting(self):
        self._reject('{"items":[[1]]}\n')       # element must be an object
        self._reject('{"items":[{"x":[1]}]}\n')  # x must be a scalar

    def test_control_char_and_bad_escape(self):
        self._reject('{"payload":"a\x01b"}\n')
        self._reject('{"payload":"a\\qb"}\n')

    def test_strict_non_nullable(self):
        self._reject('{"s":{"b":"x"},"tags":[]}\n', STRICT_SCHEMA)      # missing a
        self._reject('{"a":null,"s":{"b":"x"},"tags":[]}\n', STRICT_SCHEMA)
        self._reject('{"a":1,"s":null,"tags":[]}\n', STRICT_SCHEMA)
        self._reject('{"a":1,"s":{"b":"x"},"tags":null}\n', STRICT_SCHEMA)
        self._reject('{"a":1,"s":{"b":"x"},"tags":["a",null]}\n', STRICT_SCHEMA)
        self._reject('{"a":1,"tags":[]}\n', STRICT_SCHEMA)              # missing s
        self._reject('{"a":1,"s":{"b":"x"}}\n', STRICT_SCHEMA)          # missing tags

    def test_strict_ok_and_scalar_list_column(self):
        inp = os.path.join(self.tmp, "strict_ok.jsonl")
        with open(inp, "w") as f:
            f.write('{"a":1,"s":{"b":"x"},"tags":["a","b"]}\n')
            f.write('{"a":-2,"s":{"b":"y"},"tags":[]}\n')
        arc = os.path.join(self.tmp, "strict.ncol")
        rc, _, err = build(STRICT_SCHEMA, inp, arc)
        self.assertEqual(rc, 0, err)
        rc, out, err = scan(arc, "tags", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r["projection"] for r in json_lines(out) if "projection" in r]
        self.assertEqual(proj, [["a", "b"], []])
        rc, out, err = scan(arc, "s.b", "--eq", "y", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["projection"], {"b": "y"})


class TestMultiPageRowAndMemory(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.n_items = 100_000
        cls.big_jsonl = os.path.join(cls.tmp, "big.jsonl")
        with open(cls.big_jsonl, "w") as f:
            f.write('{"items":[' + '{"x":7},' * (cls.n_items - 1) + '{"x":7}')
            f.write('],"payload":"' + "Q" * 3_000_000 + '"}\n')
            f.write('{"items":[{"x":7}],"payload":"after"}\n')
        cls.big = os.path.join(cls.tmp, "big.ncol")
        rc, _, err = build(EVENT_SCHEMA, cls.big_jsonl, cls.big,
                           page_size=4096, rows_per_group=100)
        assert rc == 0, err

    def test_row_spans_many_pages(self):
        rc, out, _ = run_cli("dump-index", self.big)
        idx = json.loads(out)
        cols = idx["row_groups"][0]["columns"]
        self.assertGreaterEqual(len(cols["items.x"]["struct"]["pages"]), 3)
        self.assertGreaterEqual(len(cols["payload"]["values"]["pages"]), 3)

    def test_scan_multipage_row_and_following_row(self):
        rc, out, err = scan(self.big, "items.x", "--eq", "7")
        self.assertEqual(rc, 0, err)
        events = json_lines(out)
        values = [e for e in events if e["t"] == "value"]
        self.assertEqual(len(values), self.n_items + 1)   # big row + 1
        # positions keep counting up within the big row
        self.assertEqual(values[0]["pos"], [0])
        self.assertEqual(values[self.n_items - 1]["pos"], [self.n_items - 1])
        # the row after the huge row is intact
        rows = [e for e in events if e["t"] == "row_start"]
        self.assertEqual([r["row"] for r in rows], [0, 1])

    def test_is_null_no_match_reads_no_value_pages(self):
        log = os.path.join(self.tmp, "isnull_big.log")
        rc, out, err = scan(self.big, "items.x", "--is-null", page_log=log)
        self.assertEqual(rc, 0, err)
        end = json_lines(out)[-1]
        self.assertEqual(end["matched"], 0)
        pages = [l for l in open(log) if " PAGE " in l]
        self.assertTrue(pages)
        self.assertFalse(any("stream=values" in l for l in pages),
                         "is_null evaluation must not read value pages")
        self.assertFalse(any("col=payload" in l for l in pages))

    def test_memory_bounded_for_huge_row(self):
        # scan the huge row (full pass, no match) and measure peak RSS
        log = os.path.join(self.tmp, "mem.log")
        rc, out, err = scan(self.big, "items.x", "--eq", "999999",
                            "--memstats", page_log=log)
        self.assertEqual(rc, 0, err)
        peak = int(err.split("peak_rss_bytes=")[1].split()[0])
        # the row's streams total ~500KB of items.x data + 3MB payload;
        # a row-buffering scanner would need megabytes of Python objects;
        # streaming stays near the interpreter baseline
        self.assertLess(peak, 150 * 1024 * 1024)
        # and it never read a payload page
        self.assertNotIn("col=payload", open(log).read())

    def test_memory_flat_across_row_sizes(self):
        peaks = []
        for n in (20_000, 200_000):
            inp = os.path.join(self.tmp, "mem_%d.jsonl" % n)
            with open(inp, "w") as f:
                f.write('{"items":[' + '{"x":7},' * (n - 1) + '{"x":7}]}\n')
            arc = os.path.join(self.tmp, "mem_%d.ncol" % n)
            rc, _, err = build(EVENT_SCHEMA, inp, arc, page_size=4096)
            self.assertEqual(rc, 0, err)
            rc, out, err = scan(arc, "items.x", "--eq", "999999", "--memstats")
            self.assertEqual(rc, 0, err)
            peaks.append(int(err.split("peak_rss_bytes=")[1].split()[0]))
        # 10x the elements must not blow up memory
        self.assertLess(peaks[1], peaks[0] * 2 + 20 * 1024 * 1024)


class TestUnits(Base):
    def test_varint_zigzag(self):
        sys.path.insert(0, ROOT)
        from nestedcol.encoding import (BytesReader, get_uvarint, put_uvarint,
                                        zigzag_decode, zigzag_encode)
        for n in (0, 1, 127, 128, 300, 2 ** 32, 2 ** 63 - 1):
            buf = bytearray()
            put_uvarint(buf, n)
            self.assertEqual(get_uvarint(BytesReader(bytes(buf))), n)
        for n in (0, -1, 1, INT64_MIN, INT64_MAX, -5, 123456789012345):
            self.assertEqual(zigzag_decode(zigzag_encode(n)), n)

    def test_jsonsax_events(self):
        sys.path.insert(0, ROOT)
        from nestedcol.jsonsax import parse_events
        import io
        doc = b'{"a":[1,-2.5e2,true,null,"x\\u0041\\n"], "b":{}}  \n{"c":1}'
        events = list(parse_events(io.BytesIO(doc), 1 << 20))
        kinds = [e[0] for e in events]
        self.assertEqual(kinds[0], "row_begin")
        self.assertEqual(kinds[-1], "row_end")
        self.assertIn(("string", "xA\n"), events)
        self.assertIn(("number", "-2.5e2"), events)
        self.assertEqual(kinds.count("row_begin"), 2)

    def test_jsonsax_errors(self):
        sys.path.insert(0, ROOT)
        from nestedcol.errors import DataError
        from nestedcol.jsonsax import parse_events
        import io
        for bad in (b'{"a":}', b'[1,]', b'{]', b'"\\x"', b'01', b'1.',
                    b'[1', b'{"a":1', b'tru', b'nulL', b'{"a" 1}'):
            with self.assertRaises(DataError, msg=repr(bad)):
                list(parse_events(io.BytesIO(bad), 1 << 20))

    def test_max_scalar_enforced(self):
        inp = os.path.join(self.tmp, "bigscalar.jsonl")
        with open(inp, "w") as f:
            f.write('{"payload":"' + "x" * 5000 + '"}\n')
        rc, _, err = build(EVENT_SCHEMA, inp,
                           os.path.join(self.tmp, "bigscalar.ncol"),
                           max_scalar=1000)
        self.assertEqual(rc, 6)
        # but a scalar larger than a page is fine when within the limit
        rc, _, err = build(EVENT_SCHEMA, inp,
                           os.path.join(self.tmp, "bigscalar_ok.ncol"),
                           page_size=64, max_scalar=100000)
        self.assertEqual(rc, 0, err)
        rc, out, err = scan(os.path.join(self.tmp, "bigscalar_ok.ncol"),
                            "payload", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r for r in json_lines(out) if "projection" in r]
        self.assertEqual(proj[0]["projection"], "x" * 5000)


class TestSameNameFields(Base):
    """Fields with the same name/number in different structs are distinct;
    they are identified by (owning struct, number)."""

    SCHEMA = {
        "format": 1, "root": "Root",
        "structs": {
            "Root": {"fields": [
                {"num": 1, "name": "left", "type": {"kind": "struct",
                     "struct": "A", "nullable": True}},
                {"num": 2, "name": "right", "type": {"kind": "struct",
                     "struct": "B", "nullable": True}},
            ]},
            "A": {"fields": [
                {"num": 1, "name": "x", "type": {"kind": "int64", "nullable": True}},
            ]},
            "B": {"fields": [
                {"num": 1, "name": "x", "type": {"kind": "text", "nullable": True}},
            ]},
        },
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.same_schema = os.path.join(cls.tmp, "same.schema.json")
        with open(cls.same_schema, "w") as f:
            json.dump(cls.SCHEMA, f)
        cls.same_jsonl = os.path.join(cls.tmp, "same.jsonl")
        with open(cls.same_jsonl, "w") as f:
            f.write('{"left":{"x":1},"right":{"x":"one"}}\n')
            f.write('{"left":{"x":2}}\n')
            f.write('{"right":{"x":"three"}}\n')
        cls.same = os.path.join(cls.tmp, "same.ncol")
        rc, _, err = build(cls.same_schema, cls.same_jsonl, cls.same)
        assert rc == 0, err

    def test_both_paths_resolve_independently(self):
        rc, out, err = scan(self.same, "left.x", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r["projection"] for r in json_lines(out) if "projection" in r]
        self.assertEqual(proj, [{"x": 1}, {"x": 2}, {"$missing": True}])
        rc, out, err = scan(self.same, "right.x", "--assemble")
        self.assertEqual(rc, 0, err)
        proj = [r["projection"] for r in json_lines(out) if "projection" in r]
        self.assertEqual(proj, [{"x": "one"}, {"$missing": True}, {"x": "three"}])

    def test_predicates_use_owning_struct(self):
        rc, out, err = scan(self.same, "left.x", "--eq", "2", "--assemble")
        self.assertEqual(rc, 0, err)
        rows = [r["row"] for r in json_lines(out) if "projection" in r]
        self.assertEqual(rows, [1])
        rc, out, err = scan(self.same, "right.x", "--eq", "three", "--assemble")
        self.assertEqual(rc, 0, err)
        rows = [r["row"] for r in json_lines(out) if "projection" in r]
        self.assertEqual(rows, [2])


class TestProgrammaticAPI(Base):
    """The package is also a Python API: builder / reader / scanner."""

    def test_build_and_scan_in_process(self):
        sys.path.insert(0, ROOT)
        from nestedcol.reader import Archive
        from nestedcol.builder import build as nbuild
        from nestedcol.scanner import Predicate, scan_path
        from nestedcol.schema import Schema
        with open(EVENT_SCHEMA) as f:
            schema = Schema.from_dict(json.load(f))
        arc = os.path.join(self.tmp, "api.ncol")
        with open(self.events_jsonl, "rb") as inf, open(arc, "wb") as outf:
            rows = nbuild(schema, inf, outf, page_size=512, rows_per_group=10)
        self.assertEqual(rows, 15)
        import io
        out = io.StringIO()
        log_lines = []
        with Archive(arc, log=log_lines.append) as a:
            matched = scan_path(a, "items.x", Predicate(is_null=True), out,
                                assemble=True)
        self.assertEqual(matched, (15, 3))
        text = out.getvalue()
        self.assertIn('"row":3', text)
        self.assertIn('"row":4', text)
        self.assertTrue(any("LOG PAGE" in l for l in log_lines))
        self.assertFalse(any("col=payload" in l for l in log_lines))


if __name__ == "__main__":
    unittest.main()
