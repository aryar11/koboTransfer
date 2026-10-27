import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

# Crockford-style: no 0/O, 1/I/L so codes survive being read off a phone screen.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 4
COPY_CHUNK = 1024 * 1024
ORPHAN_GRACE_SECONDS = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    code           TEXT PRIMARY KEY,
    owner_token    TEXT NOT NULL,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    original_name  TEXT,
    safe_name      TEXT,
    stored_name    TEXT,
    size           INTEGER,
    uploaded_at    REAL,
    consumed_at    REAL,
    download_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS rooms_expires_at ON rooms (expires_at);
"""


class UploadTooLarge(Exception):
    pass


class RoomNotFound(Exception):
    pass


def normalize_code(raw: str | None) -> str | None:
    code = re.sub(r"[\s-]+", "", raw or "").upper()
    if len(code) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in code):
        return None
    return code


@dataclass(frozen=True)
class Room:
    code: str
    owner_token: str
    created_at: float
    expires_at: float
    original_name: str | None
    safe_name: str | None
    stored_name: str | None
    size: int | None
    uploaded_at: float | None
    consumed_at: float | None
    download_count: int

    @property
    def has_file(self) -> bool:
        return self.stored_name is not None and self.consumed_at is None

    @property
    def state(self) -> str:
        if self.consumed_at is not None:
            return "consumed"
        if self.stored_name is None:
            return "waiting"
        if self.download_count > 0:
            return "downloaded"
        return "ready"


class RoomStore:
    def __init__(self, db_path: Path, files_dir: Path, code_ttl_seconds: int, file_ttl_seconds: int):
        self.db_path = db_path
        self.files_dir = files_dir
        self.code_ttl_seconds = code_ttl_seconds
        self.file_ttl_seconds = file_ttl_seconds
        self.files_dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def file_path(self, room: Room) -> Path:
        assert room.stored_name is not None
        return self.files_dir / room.stored_name

    def _unlink(self, stored_name: str | None) -> None:
        if stored_name:
            (self.files_dir / stored_name).unlink(missing_ok=True)

    def create_room(self) -> Room:
        now = time.time()
        for _ in range(100):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            with self._conn() as conn:
                existing = conn.execute(
                    "SELECT expires_at, stored_name FROM rooms WHERE code = ?", (code,)
                ).fetchone()
                if existing is not None:
                    if existing["expires_at"] > now:
                        continue
                    self._unlink(existing["stored_name"])
                    conn.execute("DELETE FROM rooms WHERE code = ?", (code,))
                token = secrets.token_urlsafe(24)
                conn.execute(
                    "INSERT INTO rooms (code, owner_token, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (code, token, now, now + self.code_ttl_seconds),
                )
            return Room(code, token, now, now + self.code_ttl_seconds, None, None, None, None, None, None, 0)
        raise RuntimeError("Could not allocate a free room code")

    def get_active(self, code: str) -> Room | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM rooms WHERE code = ? AND expires_at > ?", (code, time.time())
            ).fetchone()
        return Room(**dict(row)) if row else None

    def get_owned(self, code: str, token: str) -> Room | None:
        room = self.get_active(code)
        if room is None or not secrets.compare_digest(room.owner_token, token):
            return None
        return room

    def extend(self, code: str, min_seconds: int) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "UPDATE rooms SET expires_at = MAX(expires_at, ?) WHERE code = ? AND expires_at > ?",
                (now + min_seconds, code, now),
            )

    def delete_room(self, code: str) -> None:
        with self._conn() as conn:
            row = conn.execute("SELECT stored_name FROM rooms WHERE code = ?", (code,)).fetchone()
            if row is None:
                return
            conn.execute("DELETE FROM rooms WHERE code = ?", (code,))
        self._unlink(row["stored_name"])

    def store_upload(
        self, code: str, original_name: str, safe_name: str, src: BinaryIO, max_bytes: int
    ) -> Room:
        if self.get_active(code) is None:
            raise RoomNotFound(code)

        stored_name = f"{code}-{secrets.token_hex(8)}.bin"
        partial = self.files_dir / f".partial-{stored_name}"
        size = 0
        try:
            with open(partial, "wb") as dst:
                while chunk := src.read(COPY_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLarge()
                    dst.write(chunk)
            os.replace(partial, self.files_dir / stored_name)
        finally:
            partial.unlink(missing_ok=True)

        now = time.time()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT stored_name FROM rooms WHERE code = ? AND expires_at > ?", (code, now)
            ).fetchone()
            if row is None:
                self._unlink(stored_name)
                raise RoomNotFound(code)
            conn.execute(
                """UPDATE rooms SET original_name = ?, safe_name = ?, stored_name = ?, size = ?,
                       uploaded_at = ?, consumed_at = NULL, download_count = 0, expires_at = ?
                   WHERE code = ?""",
                (original_name, safe_name, stored_name, size, now, now + self.file_ttl_seconds, code),
            )
        self._unlink(row["stored_name"])
        room = self.get_active(code)
        assert room is not None
        return room

    def record_download(self, code: str, stored_name: str, consume: bool) -> None:
        with self._conn() as conn:
            if consume:
                cur = conn.execute(
                    """UPDATE rooms SET download_count = download_count + 1, consumed_at = ?,
                           stored_name = NULL
                       WHERE code = ? AND stored_name = ?""",
                    (time.time(), code, stored_name),
                )
            else:
                cur = conn.execute(
                    "UPDATE rooms SET download_count = download_count + 1 WHERE code = ? AND stored_name = ?",
                    (code, stored_name),
                )
        if consume and cur.rowcount:
            self._unlink(stored_name)

    def sweep_expired(self) -> int:
        now = time.time()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT code, stored_name FROM rooms WHERE expires_at <= ?", (now,)
            ).fetchall()
            conn.execute("DELETE FROM rooms WHERE expires_at <= ?", (now,))
            referenced = {
                r["stored_name"]
                for r in conn.execute("SELECT stored_name FROM rooms WHERE stored_name IS NOT NULL")
            }
        for row in rows:
            self._unlink(row["stored_name"])

        for path in self.files_dir.iterdir():
            if path.name in referenced or not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > ORPHAN_GRACE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        return len(rows)
