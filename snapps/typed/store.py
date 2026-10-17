"""Durable append-only post log + record list (SQLite, WAL).  Shared by host, mirror, client.

Entries:  {"seq","id","link","env"}            full signed post
          {"seq","id","link","tomb":{...}}     text removed (author delete or retention);
                                               the chain still verifies because links cover ids only.
Records:  {"rseq","rid","kind":"mod"|"delete","rec":<signed record>}
"""
from __future__ import annotations

import json
import sqlite3
import threading

import mbfmt as F


class _Result:
    """Rows fetched in full while the connection lock was held."""
    __slots__ = ("rows", "rowcount")

    def __init__(self, rows, rowcount):
        self.rows, self.rowcount = rows, rowcount

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _LockedConn:
    """One sqlite connection shared by the HTTP threads and the background sync.

    sqlite3 connections are not safe for concurrent use from several threads, even with
    check_same_thread=False: interleaved cursors returned rows with env=None during a sync.
    Every statement runs and is fully fetched under one lock."""

    def __init__(self, conn, lock):
        self._c, self._lock = conn, lock

    def execute(self, sql, args=()):
        with self._lock:
            cur = self._c.execute(sql, args)
            return _Result(cur.fetchall(), cur.rowcount)

    def close(self):
        with self._lock:
            self._c.close()


class Store:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.db = _LockedConn(sqlite3.connect(path, check_same_thread=False, isolation_level=None),
                              self.lock)
        self.db.execute("PRAGMA journal_mode=WAL")
        # removed text must not linger in freed pages; redact() also checkpoints the WAL
        self.db.execute("PRAGMA secure_delete=ON")
        self.on_redact = None  # callback(pid, env_before, tomb): e.g. delete a removed blob
        self.db.execute("""CREATE TABLE IF NOT EXISTS log(
            seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, author TEXT NOT NULL,
            reply_to TEXT, ts INTEGER NOT NULL, env TEXT NOT NULL, link TEXT NOT NULL)""")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(log)")}
        if "tomb" not in cols:  # migration from phase-0 schema
            self.db.execute("ALTER TABLE log ADD COLUMN tomb TEXT")
        if "t" not in cols:  # migration to typed core: record type per entry
            self.db.execute("ALTER TABLE log ADD COLUMN t TEXT NOT NULL DEFAULT 'board.post'")
        self.db.execute("CREATE INDEX IF NOT EXISTS log_t ON log(t)")
        self.db.execute("CREATE INDEX IF NOT EXISTS log_reply ON log(reply_to)")
        self.db.execute("CREATE INDEX IF NOT EXISTS log_author ON log(author)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS records(
            rseq INTEGER PRIMARY KEY, rid TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
            rec TEXT NOT NULL)""")

    # ---- log ----
    def head(self) -> tuple[int, str]:
        r = self.db.execute("SELECT seq, link FROM log ORDER BY seq DESC LIMIT 1").fetchone()
        return (r[0], r[1]) if r else (0, F.GENESIS)

    def has(self, pid: str) -> bool:
        return self.db.execute("SELECT 1 FROM log WHERE id=?", (pid,)).fetchone() is not None

    def append(self, env: dict, meta) -> int:
        """Append an envelope already verified by a codec (meta = typed.record.Meta)."""
        with self.lock:
            seq, head = self.head()
            seq += 1
            link = F.chain_next(head, seq, meta.id)
            self.db.execute("INSERT INTO log(seq,id,author,reply_to,ts,env,link,tomb,t) "
                            "VALUES(?,?,?,?,?,?,?,NULL,?)",
                            (seq, meta.id, meta.author, meta.ref, meta.ts, json.dumps(env), link,
                             meta.t))
            return seq

    def import_entry(self, e: dict, meta=None) -> None:
        """Mirror/client path: store an entry whose seq/link (and meta) the caller verified."""
        with self.lock:
            if "tomb" in e:
                self.db.execute("INSERT INTO log(seq,id,author,reply_to,ts,env,link,tomb,t) "
                                "VALUES(?,?,?,?,?,?,?,?,?)",
                                (e["seq"], e["id"], "", None, 0, "null", e["link"],
                                 json.dumps(e["tomb"]), "tomb"))
                return
            self.db.execute("INSERT INTO log(seq,id,author,reply_to,ts,env,link,tomb,t) "
                            "VALUES(?,?,?,?,?,?,?,NULL,?)",
                            (e["seq"], e["id"], meta.author, meta.ref, meta.ts,
                             json.dumps(e["env"]), e["link"], meta.t))

    def author_of(self, pid: str):
        r = self.db.execute("SELECT author FROM log WHERE id=? AND tomb IS NULL",
                            (pid,)).fetchone()
        return r[0] if r else None

    def redact(self, pid: str, tomb: dict) -> bool:
        with self.lock:
            r = self.db.execute("SELECT env FROM log WHERE id=? AND tomb IS NULL", (pid,)).fetchone()
            if r is None:
                return False
            c = self.db.execute("UPDATE log SET env='null', tomb=? WHERE id=? AND tomb IS NULL",
                                (json.dumps(tomb), pid))
            if tomb.get("by") == "remove":  # host removal: wipe old copies out of the WAL now
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if c.rowcount and self.on_redact:
                self.on_redact(pid, json.loads(r[0]), tomb)
            return c.rowcount > 0

    def _rows(self, q, args):
        out = []
        for s, i, env, link, tomb in self.db.execute(q, args):
            e = {"seq": s, "id": i, "link": link}
            if tomb:
                e["tomb"] = json.loads(tomb)
            else:
                e["env"] = json.loads(env)
            out.append(e)
        return out

    _SEL = "SELECT seq,id,env,link,tomb FROM log "

    def since(self, seq: int, limit: int = 500):
        return self._rows(self._SEL + "WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit))

    def get(self, pid: str):
        r = self._rows(self._SEL + "WHERE id=?", (pid,))
        return r[0] if r else None

    def replies(self, pid: str):
        return self._rows(self._SEL + "WHERE reply_to=? AND tomb IS NULL ORDER BY seq", (pid,))

    def first_post_ts(self, author: str):
        r = self.db.execute("SELECT MIN(ts) FROM log WHERE author=?", (author,)).fetchone()
        return r[0]

    def older_than(self, ts: int):
        return [r[0] for r in self.db.execute(
            "SELECT id FROM log WHERE tomb IS NULL AND ts<?", (ts,))]

    # ---- records ----
    def rhead(self) -> int:
        r = self.db.execute("SELECT MAX(rseq) FROM records").fetchone()
        return r[0] or 0

    def add_record(self, kind: str, rec: dict, rid: str, rseq: int | None = None) -> int | None:
        """Append (host) or import with the primary's rseq (mirror/client).  Idempotent by rid."""
        with self.lock:
            if self.db.execute("SELECT 1 FROM records WHERE rid=?", (rid,)).fetchone():
                return None
            rseq = self.rhead() + 1 if rseq is None else rseq
            self.db.execute("INSERT INTO records VALUES(?,?,?,?)",
                            (rseq, rid, kind, json.dumps(rec)))
            return rseq

    def records_since(self, rseq: int, limit: int = 500):
        return [{"rseq": a, "rid": b, "kind": c, "rec": json.loads(d)} for a, b, c, d in
                self.db.execute("SELECT rseq,rid,kind,rec FROM records WHERE rseq>? "
                                "ORDER BY rseq LIMIT ?", (rseq, limit))]
