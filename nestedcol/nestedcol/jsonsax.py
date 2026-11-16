"""Streaming JSON parser (SAX style) used by the builder.

The parser reads the input in fixed-size chunks and emits events without
materialising the document, so a single root row may be arbitrarily large
(many megabytes, spanning many output pages).  The only buffering limits are
  * one scalar (string / number raw text) -- bounded by ``max_scalar``;
  * one Python recursion level per open container actually visited -- for
    schema-conforming input this is bounded by the schema depth, because the
    consumer (the schema-driven encoder) aborts the generator as soon as the
    structure violates the schema.

Events yielded:
  ("start_object",) ("key", str) ("end_object",)
  ("start_array",)  ("end_array",)
  ("null",) ("bool", bool) ("number", raw_text) ("string", str)

A top-level stream is a whitespace-separated sequence of JSON values
(JSONL is the common case: one value per line).  Blank lines are allowed.
Input must be UTF-8 (JSON's default); text is preserved as-is.
"""

import io

from .errors import DataError

_WS = " \t\r\n"
_DELIM = _WS + ",]}:" + "\"{[-"
_HEX = set("0123456789abcdefABCDEF")


class _Tokenizer:
    def __init__(self, fileobj, max_scalar, chunk_size=65536):
        # decode UTF-8 incrementally so multi-byte characters may straddle
        # chunk boundaries; text values are preserved exactly
        self.f = io.TextIOWrapper(fileobj, encoding="utf-8", errors="strict")
        self.max_scalar = max_scalar
        self.chunk_size = chunk_size
        self.buf = ""
        self.pos = 0
        self.line = 1
        self.col = 1
        self.eof = False

    # -- char level -------------------------------------------------------
    def _fill(self):
        if self.pos < len(self.buf) or self.eof:
            return
        try:
            data = self.f.read(self.chunk_size)
        except UnicodeDecodeError as exc:
            raise DataError("invalid UTF-8 in input at line %d: %s"
                            % (self.line, exc)) from exc
        if not data:
            self.eof = True
            self.buf = ""
            self.pos = 0
            return
        self.buf = data
        self.pos = 0

    def peek(self):
        self._fill()
        if self.pos >= len(self.buf):
            return ""
        return self.buf[self.pos]

    def next(self):
        self._fill()
        if self.pos >= len(self.buf):
            return ""
        c = self.buf[self.pos]
        self.pos += 1
        if c == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return c

    def error(self, msg):
        raise DataError("JSON syntax error at line %d col %d: %s"
                        % (self.line, self.col, msg))

    # -- token level ------------------------------------------------------
    def skip_ws(self):
        while True:
            c = self.peek()
            if c and c in _WS:
                self.next()
            else:
                return

    def next_token(self):
        """Returns one of: '{', '}', '[', ']', ':', ',',
        ('string', s), ('number', raw), ('literal', value) or None at EOF."""
        self.skip_ws()
        c = self.next()
        if c == "":
            return None
        if c in "{}[]:,":
            return c
        if c == '"':
            return ("string", self._string())
        if c == "-" or c.isdigit():
            return ("number", self._number(c))
        if c == "t":
            self._word("rue")
            return ("literal", True)
        if c == "f":
            self._word("alse")
            return ("literal", False)
        if c == "n":
            self._word("ull")
            return ("literal", None)
        self.error("unexpected character %r" % c)

    def _word(self, rest):
        for want in rest:
            got = self.next()
            if got != want:
                self.error("bad literal")
        c = self.peek()
        if c and (c.isalnum() or c in "_+-."):
            self.error("bad literal")

    def _string(self):
        # accumulate in parts and join periodically: a large scalar stays
        # within ~2x its own size instead of millions of 1-char objects
        parts = []
        cur = []
        cur_len = 0
        total = 0
        while True:
            c = self.next()
            if c == "":
                self.error("unterminated string")
            if c == '"':
                break
            if c == "\\":
                e = self.next()
                if e == "":
                    self.error("unterminated escape")
                if e in '"\\/':
                    cur.append(e)
                elif e == "b":
                    cur.append("\b")
                elif e == "f":
                    cur.append("\f")
                elif e == "n":
                    cur.append("\n")
                elif e == "r":
                    cur.append("\r")
                elif e == "t":
                    cur.append("\t")
                elif e == "u":
                    cur.append(self._unicode_escape())
                else:
                    self.error("bad escape \\%s" % e)
                cur_len += 1
            elif ord(c) < 0x20:
                self.error("unescaped control character in string")
            else:
                cur.append(c)
                cur_len += 1
            if cur_len >= 8192:
                parts.append("".join(cur))
                total += cur_len
                cur = []
                cur_len = 0
                # decoded chars <= utf-8 bytes, so this is a safe early guard
                if total > self.max_scalar:
                    self.error("string scalar exceeds max scalar size")
        parts.append("".join(cur))
        s = "".join(parts)
        if len(s.encode("utf-8")) > self.max_scalar:
            self.error("string scalar exceeds max scalar size")
        return s

    def _unicode_escape(self):
        digits = []
        for _ in range(4):
            d = self.next()
            if d not in _HEX:
                self.error("bad \\u escape")
            digits.append(d)
        cp = int("".join(digits), 16)
        if 0xD800 <= cp <= 0xDBFF:
            # high surrogate: require a low surrogate pair
            if self.next() != "\\" or self.next() != "u":
                self.error("lone high surrogate")
            digits = []
            for _ in range(4):
                d = self.next()
                if d not in _HEX:
                    self.error("bad \\u escape")
                digits.append(d)
            lo = int("".join(digits), 16)
            if not (0xDC00 <= lo <= 0xDFFF):
                self.error("bad low surrogate")
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
        elif 0xDC00 <= cp <= 0xDFFF:
            self.error("lone low surrogate")
        return chr(cp)

    def _number(self, first):
        raw = [first]
        if first == "-":
            d = self.next()
            if not d.isdigit():
                self.error("bad number")
            raw.append(d)
            first = d
        # integer part: `first` is a digit here
        if first == "0":
            if self.peek().isdigit():
                self.error("leading zero in number")
        else:
            while self.peek().isdigit():
                raw.append(self.next())
        # fraction
        if self.peek() == ".":
            raw.append(self.next())
            if not self.peek().isdigit():
                self.error("bad number")
            while self.peek().isdigit():
                raw.append(self.next())
        # exponent
        if self.peek() in ("e", "E"):
            raw.append(self.next())
            if self.peek() in ("+", "-"):
                raw.append(self.next())
            if not self.peek().isdigit():
                self.error("bad number")
            while self.peek().isdigit():
                raw.append(self.next())
        text = "".join(raw)
        if len(text) > self.max_scalar:
            self.error("number scalar exceeds max scalar size")
        c = self.peek()
        if c and c not in _WS + ",]}":
            self.error("bad character after number")
        return text


def parse_events(fileobj, max_scalar):
    """Generator of SAX events for a stream of top-level JSON values."""
    tz = _Tokenizer(fileobj, max_scalar)
    depth_guard = [0]

    def parse_value(first_tok):
        depth_guard[0] += 1
        if depth_guard[0] > 1_000_000:           # parser safety guard only
            tz.error("nesting too deep")
        try:
            if first_tok == "{":
                yield ("start_object",)
                t = tz.next_token()
                if t == "}":
                    yield ("end_object",)
                    return
                while True:
                    if not (isinstance(t, tuple) and t[0] == "string"):
                        tz.error("object key must be a string")
                    yield ("key", t[1])
                    if tz.next_token() != ":":
                        tz.error("expected ':'")
                    v = tz.next_token()
                    if v is None:
                        tz.error("unexpected end of input in object")
                    yield from parse_value(v)
                    t = tz.next_token()
                    if t == ",":
                        t = tz.next_token()
                        continue
                    if t == "}":
                        yield ("end_object",)
                        return
                    tz.error("expected ',' or '}'")
            elif first_tok == "[":
                yield ("start_array",)
                t = tz.next_token()
                if t == "]":
                    yield ("end_array",)
                    return
                while True:
                    if t is None:
                        tz.error("unexpected end of input in array")
                    yield from parse_value(t)
                    t = tz.next_token()
                    if t == ",":
                        t = tz.next_token()
                        continue
                    if t == "]":
                        yield ("end_array",)
                        return
                    tz.error("expected ',' or ']'")
            elif isinstance(first_tok, tuple):
                kind, val = first_tok
                if kind == "string":
                    yield ("string", val)
                elif kind == "number":
                    yield ("number", val)
                else:  # literal
                    if val is None:
                        yield ("null",)
                    else:
                        yield ("bool", val)
            else:
                tz.error("expected a JSON value")
        finally:
            depth_guard[0] -= 1

    while True:
        t = tz.next_token()
        if t is None:
            return
        yield ("row_begin",)
        yield from parse_value(t)
        yield ("row_end",)
