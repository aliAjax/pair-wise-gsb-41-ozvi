"""逐项定损与共保结算的存储层（SQLite）。

只负责 settlement_* 四张表的读写与冻结快照，不做赔款计算
（计算见 settlement_calc），也不感知 HTTP（页面见 static/settlement.html）。

版本模型：
- 每案可有多版定损（settlement_versions），同一时刻至多一版 draft；
- draft 版可追加定损项、整组替换共保份额、复核定损项；
- 核定时把 draft 的定损项/份额打上 version_id 并生成 settlement_lines 快照，
  版本转为 finalized，此后该版全部记录只读；
- 定损项与共保人一旦归属某个 finalized 版本即不可改不可删，旧结算永久可查。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    total_payout REAL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finalized_by TEXT,
    finalized_at TEXT,
    UNIQUE(claim_id, version_no)
);
CREATE TABLE IF NOT EXISTS settlement_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    version_id INTEGER REFERENCES settlement_versions(id),
    item_name TEXT NOT NULL,
    insured_amount REAL NOT NULL,
    loss_ratio REAL NOT NULL,
    salvage_value REAL NOT NULL DEFAULT 0,
    deductible REAL NOT NULL DEFAULT 0,
    payout REAL NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_coinsurers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    version_id INTEGER REFERENCES settlement_versions(id),
    insurer TEXT NOT NULL,
    share_pct REAL NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    version_id INTEGER NOT NULL REFERENCES settlement_versions(id),
    item_name TEXT NOT NULL,
    item_payout REAL NOT NULL,
    insurer TEXT NOT NULL,
    share_pct REAL NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlement_items_claim ON settlement_items(claim_id, version_id);
CREATE INDEX IF NOT EXISTS idx_settlement_shares_claim ON settlement_coinsurers(claim_id, version_id);
CREATE INDEX IF NOT EXISTS idx_settlement_lines_version ON settlement_lines(claim_id, version_id);
"""


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SettlementStore:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---- 基础读取 ----

    def claim_row(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise StoreError("理赔案件不存在", 404)
        return row

    def current_version(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM settlement_versions WHERE claim_id=? ORDER BY version_no DESC LIMIT 1",
            (claim_id,),
        ).fetchone()

    def version_row(self, conn: sqlite3.Connection, claim_id: int, version_no: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM settlement_versions WHERE claim_id=? AND version_no=?",
            (claim_id, version_no),
        ).fetchone()
        if not row:
            raise StoreError("定损版本不存在", 404)
        return row

    def list_versions(self, conn: sqlite3.Connection, claim_id: int) -> list[dict[str, Any]]:
        self.claim_row(conn, claim_id)
        rows = conn.execute(
            "SELECT * FROM settlement_versions WHERE claim_id=? ORDER BY version_no DESC",
            (claim_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def version_detail(self, conn: sqlite3.Connection, claim_id: int, version_no: int) -> dict[str, Any]:
        version = self.version_row(conn, claim_id, version_no)
        return self._detail(conn, claim_id, dict(version))

    def current_detail(self, conn: sqlite3.Connection, claim_id: int) -> dict[str, Any] | None:
        version = self.current_version(conn, claim_id)
        if not version:
            return None
        return self._detail(conn, claim_id, dict(version))

    def _detail(self, conn: sqlite3.Connection, claim_id: int, version: dict[str, Any]) -> dict[str, Any]:
        if version["status"] == "finalized":
            item_sql = "SELECT * FROM settlement_items WHERE claim_id=? AND version_id=? ORDER BY id"
            share_sql = "SELECT * FROM settlement_coinsurers WHERE claim_id=? AND version_id=? ORDER BY id"
            args = (claim_id, version["id"])
        else:
            item_sql = "SELECT * FROM settlement_items WHERE claim_id=? AND version_id IS NULL ORDER BY id"
            share_sql = "SELECT * FROM settlement_coinsurers WHERE claim_id=? AND version_id IS NULL ORDER BY id"
            args = (claim_id,)
        items = [dict(r) for r in conn.execute(item_sql, args).fetchall()]
        shares = [dict(r) for r in conn.execute(share_sql, args).fetchall()]
        lines = [dict(r) for r in conn.execute(
            "SELECT * FROM settlement_lines WHERE claim_id=? AND version_id=? ORDER BY id",
            (claim_id, version["id"]),
        ).fetchall()]
        return {"version": version, "items": items, "shares": shares, "lines": lines}

    # ---- 草稿维护（要求当前版本为 draft） ----

    def _require_draft(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
        version = self.current_version(conn, claim_id)
        if not version:
            raise StoreError("尚无定损版本，请先创建", 409)
        if version["status"] != "draft":
            raise StoreError("当前定损版本已核定冻结，请创建新版本后再修改", 409)
        return version

    def create_version(self, conn: sqlite3.Connection, claim_id: int,
                       actor: str, note: str, now: str) -> dict[str, Any]:
        self.claim_row(conn, claim_id)
        current = self.current_version(conn, claim_id)
        if current and current["status"] == "draft":
            raise StoreError("已存在未核定的定损版本（v%d），请先核定或继续编辑" % current["version_no"], 409)
        next_no = (current["version_no"] + 1) if current else 1
        cur = conn.execute(
            "INSERT INTO settlement_versions(claim_id,version_no,status,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (claim_id, next_no, "draft", note.strip(), actor, now),
        )
        return dict(conn.execute("SELECT * FROM settlement_versions WHERE id=?", (cur.lastrowid,)).fetchone())

    def add_item(self, conn: sqlite3.Connection, claim_id: int, fields: dict[str, Any],
                 payout: float, actor: str, now: str) -> dict[str, Any]:
        self._require_draft(conn, claim_id)
        cur = conn.execute(
            """INSERT INTO settlement_items(claim_id,item_name,insured_amount,loss_ratio,salvage_value,
               deductible,payout,review_status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (claim_id, fields["item_name"], fields["insured_amount"], fields["loss_ratio"],
             fields["salvage_value"], fields["deductible"], payout, "draft", actor, now),
        )
        return dict(conn.execute("SELECT * FROM settlement_items WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_item_review(self, conn: sqlite3.Connection, claim_id: int, item_id: int,
                        status: str, actor: str, now: str) -> dict[str, Any]:
        self._require_draft(conn, claim_id)
        row = conn.execute(
            "SELECT * FROM settlement_items WHERE id=? AND claim_id=? AND version_id IS NULL",
            (item_id, claim_id),
        ).fetchone()
        if not row:
            raise StoreError("定损项不存在或已冻结", 404)
        conn.execute("UPDATE settlement_items SET review_status=? WHERE id=?", (status, item_id))
        return dict(conn.execute("SELECT * FROM settlement_items WHERE id=?", (item_id,)).fetchone())

    def remove_item(self, conn: sqlite3.Connection, claim_id: int, item_id: int) -> dict[str, Any]:
        """删除草稿版中的定损项（录错项的修正路径）；已冻结项拒绝删除。"""
        self._require_draft(conn, claim_id)
        row = conn.execute(
            "SELECT * FROM settlement_items WHERE id=? AND claim_id=? AND version_id IS NULL",
            (item_id, claim_id),
        ).fetchone()
        if not row:
            raise StoreError("定损项不存在或已冻结", 404)
        conn.execute("DELETE FROM settlement_items WHERE id=?", (item_id,))
        return dict(row)

    def replace_shares(self, conn: sqlite3.Connection, claim_id: int,
                       shares: list[dict[str, Any]], actor: str, now: str) -> list[dict[str, Any]]:
        self._require_draft(conn, claim_id)
        conn.execute("DELETE FROM settlement_coinsurers WHERE claim_id=? AND version_id IS NULL", (claim_id,))
        for share in shares:
            conn.execute(
                "INSERT INTO settlement_coinsurers(claim_id,insurer,share_pct,created_by,created_at) VALUES(?,?,?,?,?)",
                (claim_id, share["insurer"], share["share_pct"], actor, now),
            )
        return [dict(r) for r in conn.execute(
            "SELECT * FROM settlement_coinsurers WHERE claim_id=? AND version_id IS NULL ORDER BY id", (claim_id,)
        ).fetchall()]

    # ---- 核定：冻结快照 ----

    def finalize_snapshot(self, conn: sqlite3.Connection, claim_id: int,
                          lines: list[dict[str, Any]], total_payout: float,
                          actor: str, now: str) -> dict[str, Any]:
        version = self._require_draft(conn, claim_id)
        vid = version["id"]
        conn.execute(
            "UPDATE settlement_items SET version_id=? WHERE claim_id=? AND version_id IS NULL",
            (vid, claim_id),
        )
        conn.execute(
            "UPDATE settlement_coinsurers SET version_id=? WHERE claim_id=? AND version_id IS NULL",
            (vid, claim_id),
        )
        for line in lines:
            conn.execute(
                """INSERT INTO settlement_lines(claim_id,version_id,item_name,item_payout,insurer,share_pct,amount,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (claim_id, vid, line["item_name"], line["item_payout"], line["insurer"],
                 line["share_pct"], line["amount"], now),
            )
        conn.execute(
            "UPDATE settlement_versions SET status='finalized',total_payout=?,finalized_by=?,finalized_at=? WHERE id=?",
            (total_payout, actor, now, vid),
        )
        return self.version_detail(conn, claim_id, version["version_no"])
