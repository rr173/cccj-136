"""Low-level encodings: unsigned varints, zig-zag int64, CRC32, structure
tokens and leaf value codecs.

Everything is little-endian.  A "reader" is any object with
``read(n) -> bytes`` (exactly n bytes or raise) and ``read_byte() -> int``;
``reader.StreamReader`` and the ``BytesReader`` below both qualify.
"""

import zlib

from .errors import PageCorrupt

# ---------------------------------------------------------------- tokens
# Structure tokens of a leaf column's struct stream.  One unsigned varint
# per token (all currently fit in one byte).
TOK_MISS = 0     # struct field is absent from the object (nullable fields only)
TOK_NULL = 1     # explicit JSON null at this level
TOK_PRESENT = 2  # struct (field or element) is present -> descend
TOK_EMPTY = 3    # list is present and has zero elements
TOK_BEGIN = 4    # list is present and non-empty; elements follow, then TOK_END
TOK_END = 5      # end of a non-empty list's elements
TOK_VALUE = 6    # leaf has a value; the value is appended to the value stream

TOK_NAMES = {
    TOK_MISS: "MISS",
    TOK_NULL: "NULL",
    TOK_PRESENT: "PRESENT",
    TOK_EMPTY: "EMPTY",
    TOK_BEGIN: "BEGIN",
    TOK_END: "END",
    TOK_VALUE: "VALUE",
}

# ------------------------------------------------------------- primitives

def put_uvarint(buf: bytearray, n: int) -> None:
    if n < 0:
        raise ValueError("uvarint of negative")
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def get_uvarint(reader) -> int:
    shift = 0
    result = 0
    while True:
        b = reader.read_byte()
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result
        shift += 7
        if shift > 70:
            raise PageCorrupt("varint too long")


def zigzag_encode(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def zigzag_decode(z: int) -> int:
    return (z >> 1) ^ -(z & 1)


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

# -------------------------------------------------------------- leaf values
# Leaf kinds: "int64", "text", "bool".

def encode_value(buf: bytearray, kind: str, value) -> None:
    if kind == "int64":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("int64 expected")
        if not (INT64_MIN <= value <= INT64_MAX):
            raise ValueError("int64 out of range")
        put_uvarint(buf, zigzag_encode(value))
    elif kind == "bool":
        buf.append(1 if value else 0)
    elif kind == "text":
        data = value.encode("utf-8")
        put_uvarint(buf, len(data))
        buf += data
    else:  # pragma: no cover - schema validation prevents this
        raise ValueError("unknown leaf kind %r" % kind)


def decode_value(reader, kind: str):
    if kind == "int64":
        return zigzag_decode(get_uvarint(reader))
    if kind == "bool":
        b = reader.read_byte()
        if b > 1:
            raise PageCorrupt("invalid bool byte %d" % b)
        return bool(b)
    if kind == "text":
        n = get_uvarint(reader)
        data = reader.read(n)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PageCorrupt("invalid utf-8 in text value") from exc
    raise PageCorrupt("unknown leaf kind %r" % kind)


class BytesReader:
    """Reader over an in-memory bytes object (used by the checker and tests)."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def read(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise PageCorrupt("unexpected end of buffer")
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def read_byte(self) -> int:
        if self.pos >= len(self.data):
            raise PageCorrupt("unexpected end of buffer")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def at_end(self) -> bool:
        return self.pos >= len(self.data)
