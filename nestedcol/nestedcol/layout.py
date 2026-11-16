"""On-disk layout constants shared by builder, reader and checker.

File layout (all integers little-endian)::

  MAGIC_H            8 bytes   b"NCOL0001"
  u32 header_len
  header_json        header_len bytes   (UTF-8 JSON: format, schema, columns)
  u32 header_crc32   CRC32 of header_json

  row group 0
  row group 1
  ...

  footer_json        footer_json_len bytes
  u32 footer_crc32   CRC32 of footer_json
  u32 footer_json_len
  MAGIC_F            8 bytes   b"NCOLEND1"

Row group layout::

  page records of all columns (struct kind=1, values kind=2), physically
      interleaved in flush order -- the builder writes pages as stream
      buffers fill, so no whole row group is ever buffered;
  one row-index record per leaf column (page record, kind=3);
  u32 meta_json_len
  meta_json          (page directories of every column, row count, ...)
  u32 meta_crc32

The meta_json page directories map each logical stream to its pages; the
records of a row group tile [rg.offset, rg.meta_offset) exactly.

Page record (13-byte header + payload)::

  u8  stream_kind     1=struct 2=values 3=row_index
  u32 page_index      0-based within the stream
  u32 payload_len
  u32 crc32           over (kind||page_index||payload_len||payload)
  payload
"""

MAGIC_H = b"NCOL0001"
MAGIC_F = b"NCOLEND1"
FORMAT_VERSION = 1

PAGE_STRUCT = 1
PAGE_VALUES = 2
PAGE_ROW_INDEX = 3

PAGE_HEADER_LEN = 13  # 1 + 4 + 4 + 4
