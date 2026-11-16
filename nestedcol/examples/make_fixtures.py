"""Deterministic fixture generator.

Usage: python3 make_fixtures.py OUTDIR [big]

Writes:
  five.jsonl     exactly the five fixed rows (missing / null / [] / [null] /
                 [null,7,7] with a large payload)
  events.jsonl   the five fixed rows plus coverage rows (nested lists,
                 nullable struct, int64 extremes, long unicode text)
  edge.jsonl     40 rows {"items":[]} -- with --page-size 8 every 8th root
                 row boundary lands exactly on a page boundary
  bigrow.jsonl   (only with `big`) one row with 1,000,000 items plus a ~5 MB
                 payload, followed by a small row
"""

import json
import os
import sys

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_events(path):
    payload5 = "PAYLOAD-" * 7500          # ~60 KB, spans many small pages
    long_text = "长文本-" + "é" * 5000 + "-tail"
    rows = five_rows(payload5) + [
        {"matrix": [[None], []], "meta": {"flag": True}, "payload": "p6"},
        {"matrix": [None], "meta": {}},
        {"matrix": [[{"v": -5, "tag": "neg"}], [None, {"v": 9}]]},
        {"items": [{"x": INT64_MIN}], "meta": {"flag": False}},
        {"payload": long_text},
        {"items": [{"x": INT64_MAX}]},
        {"matrix": [[{"v": 0}]], "meta": {"flag": None}},
        {"items": [{}]},
        {"items": [{"x": None}]},
        {"items": [{"x": 7}]},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(_dump(r) + "\n")
    return len(rows)


def five_rows(payload):
    """The five fixed rows, in order."""
    return [
        {},                                                     # 1 missing items
        {"items": None},                                        # 2 items=null
        {"items": []},                                          # 3 items=[]
        {"items": [None]},                                      # 4 items=[null]
        {"items": [{"x": None}, {"x": 7}, {"x": 7}],
         "payload": payload},                                   # 5 + large payload
    ]


def write_five(path):
    with open(path, "w", encoding="utf-8") as f:
        for r in five_rows("PAYLOAD-" * 7500):
            f.write(_dump(r) + "\n")
    return 5


def write_edge(path, n=40):
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(n):
            f.write('{"items":[]}\n')
    return n


def write_bigrow(path, n_items=1_000_000, payload_len=5_000_000):
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"items":[')
        f.write('{"x":7}')
        for _ in range(n_items - 1):
            f.write(',{"x":7}')
        f.write('],"payload":"')
        # write the payload in chunks: bounded memory even for huge scalars
        chunk = "Q" * 1_000_000
        left = payload_len
        while left > 0:
            take = min(left, len(chunk))
            f.write(chunk[:take])
            left -= take
        f.write('"}\n')
        f.write('{"items":[{"x":1}],"payload":"after"}\n')
    return 2


def main():
    outdir = sys.argv[1]
    big = len(sys.argv) > 2 and sys.argv[2] == "big"
    os.makedirs(outdir, exist_ok=True)
    n = write_five(os.path.join(outdir, "five.jsonl"))
    print("five.jsonl: %d rows" % n)
    n = write_events(os.path.join(outdir, "events.jsonl"))
    print("events.jsonl: %d rows" % n)
    n = write_edge(os.path.join(outdir, "edge.jsonl"))
    print("edge.jsonl: %d rows" % n)
    if big:
        n = write_bigrow(os.path.join(outdir, "bigrow.jsonl"))
        print("bigrow.jsonl: %d rows" % n)


if __name__ == "__main__":
    main()
