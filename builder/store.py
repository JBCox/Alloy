"""SQLite store: versioned records, record history, append-only journal, file registry, and
control requests (spec D5, R-2, R-3, R-45, R-59).

Every ``upsert`` is one transaction: history row for the superseded version, the new record
row, and a journal row that carries the full ``after`` snapshot. Because every journal row
carries the snapshot, ``rebuild_from_journal`` can reproduce the ``records`` table from the
journal alone.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ids import sha256_file, utc_now
from .records import Record, validate_record

SCHEMA_VERSION = "1"

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT, parent_id TEXT,
    data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS records_kind_state ON records(kind, state);
CREATE INDEX IF NOT EXISTS records_kind_parent ON records(kind, parent_id);
CREATE TABLE IF NOT EXISTS record_history (
    kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT, parent_id TEXT,
    data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, changed_at TEXT NOT NULL,
    journal_seq INTEGER, PRIMARY KEY (kind, id, version));
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id TEXT, actor TEXT NOT NULL,
    actor_kind TEXT NOT NULL, event TEXT NOT NULL, record_kind TEXT, record_id TEXT,
    from_state TEXT, to_state TEXT, inputs TEXT, evidence TEXT, outcome TEXT, op_id TEXT, after TEXT);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
    BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
    BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL, kind TEXT NOT NULL,
    record_id TEXT, registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS control_requests (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    consumed_at TEXT);
"""


@dataclass
class JournalEntry:
    seq: int
    ts: str
    run_id: str | None
    actor: str
    actor_kind: str
    event: str
    record_kind: str | None
    record_id: str | None
    from_state: str | None
    to_state: str | None
    inputs: dict[str, Any] | None
    evidence: list[Any] | None
    outcome: str | None
    op_id: str | None
    after: dict[str, Any] | None


@dataclass
class ControlRequest:
    seq: int
    ts: str
    kind: str
    payload: dict[str, Any]


def actor_kind_of(actor: str) -> str:
    head = actor.split(":", 1)[0]
    if head == "engine":
        return "engine"
    if head == "user":
        return "user"
    if head == "agent":
        return "agent"
    return "other"


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # --- lifecycle ---------------------------------------------------------------

    def open(self) -> "Store":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_DDL)
        existing = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if existing is None:
            conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            conn.execute("INSERT INTO meta(key, value) VALUES ('created_at', ?)", (utc_now(),))
        elif existing["value"] != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported schema_version {existing['value']} (expected {SCHEMA_VERSION})")
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not open")
        return self._conn

    def journal_mode(self) -> str:
        return self.conn.execute("PRAGMA journal_mode").fetchone()[0]

    def raw_execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    # --- meta ----------------------------------------------------------------------

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # --- records -------------------------------------------------------------------

    def upsert(self, record: Record, *, actor: str, event: str, inputs: dict[str, Any] | None = None,
               evidence: list[Any] | None = None, outcome: str | None = None, op_id: str | None = None,
               run_id: str | None = None) -> Record:
        """Validate, version, journal, and store ``record`` atomically. Returns the same object updated."""
        validate_record(record)
        now = utc_now()
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute("SELECT * FROM records WHERE kind=? AND id=?",
                                        (record.kind, record.id)).fetchone()
                from_state = None
                if existing is not None:
                    from_state = existing["state"]
                    record.version = int(existing["version"]) + 1
                    record.created_at = existing["created_at"]
                else:
                    record.version = 1
                    record.created_at = now
                record.updated_at = now
                seq = self._append_journal_locked(
                    actor=actor, event=event, record_kind=record.kind, record_id=record.id,
                    from_state=from_state, to_state=record.state, inputs=inputs, evidence=evidence,
                    outcome=outcome, op_id=op_id, run_id=run_id, after=record.to_json())
                if existing is not None:
                    conn.execute(
                        "INSERT INTO record_history(kind,id,version,state,parent_id,data,created_at,updated_at,"
                        "changed_at,journal_seq) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (existing["kind"], existing["id"], existing["version"], existing["state"],
                         existing["parent_id"], existing["data"], existing["created_at"], existing["updated_at"],
                         now, seq))
                conn.execute(
                    "INSERT OR REPLACE INTO records(kind,id,version,state,parent_id,data,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (record.kind, record.id, record.version, record.state, record.parent_id,
                     json.dumps(record.data, ensure_ascii=False), record.created_at, record.updated_at))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return record

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> Record:
        return Record(kind=row["kind"], id=row["id"], data=json.loads(row["data"]), state=row["state"],
                      parent_id=row["parent_id"], version=int(row["version"]),
                      created_at=row["created_at"], updated_at=row["updated_at"])

    def get(self, kind: str, id: str) -> Record | None:
        row = self.conn.execute("SELECT * FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return None if row is None else self._row_to_record(row)

    def require(self, kind: str, id: str) -> Record:
        rec = self.get(kind, id)
        if rec is None:
            raise KeyError(f"{kind} {id} not found")
        return rec

    def list(self, kind: str, *, state: str | None = None, parent_id: str | None = None) -> list[Record]:
        sql = "SELECT * FROM records WHERE kind=?"
        params: list[Any] = [kind]
        if state is not None:
            sql += " AND state=?"
            params.append(state)
        if parent_id is not None:
            sql += " AND parent_id=?"
            params.append(parent_id)
        sql += " ORDER BY created_at, id"
        return [self._row_to_record(r) for r in self.conn.execute(sql, params)]

    def list_all(self) -> list[Record]:
        return [self._row_to_record(r) for r in self.conn.execute("SELECT * FROM records ORDER BY kind, id")]

    def history(self, kind: str, id: str) -> list[Record]:
        rows = self.conn.execute("SELECT * FROM record_history WHERE kind=? AND id=? ORDER BY version", (kind, id))
        return [self._row_to_record(r) for r in rows]

    # --- journal -------------------------------------------------------------------

    def _append_journal_locked(self, *, actor: str, event: str, record_kind: str | None = None,
                               record_id: str | None = None, from_state: str | None = None,
                               to_state: str | None = None, inputs: dict[str, Any] | None = None,
                               evidence: list[Any] | None = None, outcome: str | None = None,
                               op_id: str | None = None, run_id: str | None = None,
                               after: dict[str, Any] | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO journal(ts,run_id,actor,actor_kind,event,record_kind,record_id,from_state,to_state,"
            "inputs,evidence,outcome,op_id,after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utc_now(), run_id, actor, actor_kind_of(actor), event, record_kind, record_id, from_state, to_state,
             None if inputs is None else json.dumps(inputs, ensure_ascii=False),
             None if evidence is None else json.dumps(evidence, ensure_ascii=False),
             outcome, op_id, None if after is None else json.dumps(after, ensure_ascii=False)))
        return int(cur.lastrowid)

    def append_journal(self, *, actor: str, event: str, **kw: Any) -> int:
        with self._lock:
            return self._append_journal_locked(actor=actor, event=event, **kw)

    def journal(self, since_seq: int = 0, limit: int | None = None) -> list[JournalEntry]:
        sql = "SELECT * FROM journal WHERE seq>? ORDER BY seq"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        out = []
        for r in self.conn.execute(sql, (since_seq,)):
            out.append(JournalEntry(
                seq=r["seq"], ts=r["ts"], run_id=r["run_id"], actor=r["actor"], actor_kind=r["actor_kind"],
                event=r["event"], record_kind=r["record_kind"], record_id=r["record_id"],
                from_state=r["from_state"], to_state=r["to_state"], inputs=_loads(r["inputs"]),
                evidence=_loads(r["evidence"]), outcome=r["outcome"], op_id=r["op_id"], after=_loads(r["after"])))
        return out

    def last_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) FROM journal").fetchone()
        return int(row[0] or 0)

    def rebuild_from_journal(self) -> dict[tuple[str, str], Record]:
        rebuilt: dict[tuple[str, str], Record] = {}
        for e in self.journal():
            if e.record_kind and e.record_id and e.after:
                rebuilt[(e.record_kind, e.record_id)] = Record.from_json(e.after)
        return rebuilt

    def verify_rebuild(self) -> bool:
        rebuilt = self.rebuild_from_journal()
        current = {(r.kind, r.id): r for r in self.list_all()}
        if set(rebuilt) != set(current):
            return False
        return all(rebuilt[k].to_json() == current[k].to_json() for k in current)

    # --- files ---------------------------------------------------------------------

    @staticmethod
    def _key(path: str | Path) -> str:
        return str(Path(path).resolve())

    def register_file(self, path: str | Path, *, kind: str, record_id: str | None = None) -> str:
        p = Path(path)
        digest = sha256_file(p)
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO files(path,sha256,size,kind,record_id,registered_at) VALUES (?,?,?,?,?,?)",
                (self._key(p), digest, p.stat().st_size, kind, record_id, utc_now()))
        return digest

    def file_entry(self, path: str | Path) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM files WHERE path=?", (self._key(path),)).fetchone()
        return None if row is None else dict(row)

    def verify_file(self, path: str | Path) -> tuple[bool, str | None, str | None]:
        """Returns (ok, expected_sha256, actual_sha256). ``actual`` is None when the file is missing."""
        entry = self.file_entry(path)
        expected = None if entry is None else entry["sha256"]
        p = Path(path)
        if not p.is_file():
            return False, expected, None
        actual = sha256_file(p)
        return (expected is not None and actual == expected), expected, actual

    # --- control requests ------------------------------------------------------------

    def push_control(self, kind: str, payload: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO control_requests(ts,kind,payload) VALUES (?,?,?)",
                                    (utc_now(), kind, json.dumps(payload or {}, ensure_ascii=False)))
            return int(cur.lastrowid)

    def pop_controls(self) -> list[ControlRequest]:
        with self._lock:
            rows = list(self.conn.execute(
                "SELECT * FROM control_requests WHERE consumed_at IS NULL ORDER BY seq"))
            now = utc_now()
            out = []
            for r in rows:
                self.conn.execute("UPDATE control_requests SET consumed_at=? WHERE seq=?", (now, r["seq"]))
                out.append(ControlRequest(seq=r["seq"], ts=r["ts"], kind=r["kind"], payload=json.loads(r["payload"])))
            return out


def db_size_bytes(path: str | Path) -> int:
    p = Path(path)
    total = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            total += os.path.getsize(f)
    return total
