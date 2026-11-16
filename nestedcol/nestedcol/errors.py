"""Error types for nestedcol.  Each carries the process exit code the CLI uses.

Exit code contract (documented in README.md):
  0  success
  1  unexpected internal error
  2  command line usage error
  3  archive index / metadata corrupt or invalid (header, footer, row-group
     meta, row-index).  The archive is *rejected* before any scan.
  4  a data page that was actually accessed is corrupt (bad checksum,
     short read, undecodable stream)
  5  schema / path / predicate problem
  6  input data invalid (build-time JSON or type errors)
"""


class NcolError(Exception):
    exit_code = 1


class UsageError(NcolError):
    exit_code = 2


class IndexCorrupt(NcolError):
    """Root row-group index, header, footer, row-group meta or row-index
    is damaged.  The archive must be rejected."""
    exit_code = 3


class PageCorrupt(NcolError):
    """A data page that was accessed failed verification."""
    exit_code = 4


class SchemaError(NcolError):
    exit_code = 5


class DataError(NcolError):
    """Input JSONL violates the schema or JSON syntax at build time."""
    exit_code = 6
