"""Isolated A8 experiment: fail closed if pinned SQLite cannot be loaded."""
import ctypes
import hashlib
import os
from pathlib import Path
import sys

EXPECTED_SHA256 = "168e48ea59c72fcbe3c4d5a92dcffc4f338775e3879265f065198c617e4ac1ae"
try:
    library = Path(sys.prefix) / "lib/libsqlite3-a8.so"
    if hashlib.sha256(library.read_bytes()).hexdigest() != EXPECTED_SHA256:
        raise RuntimeError("SQLite library digest mismatch")
    _library = ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
    import sqlite3
    if sqlite3.sqlite_version != "3.54.1":
        raise RuntimeError(f"Unexpected SQLite: {sqlite3.sqlite_version}")
    with sqlite3.connect(":memory:") as connection:
        if connection.execute("SELECT sqlite_version()").fetchone()[0] != "3.54.1":
            raise RuntimeError("SQL engine version mismatch")
except BaseException as error:
    os.write(2, f"A8 SQLite bootstrap refused startup: {error}\n".encode())
    os._exit(78)
