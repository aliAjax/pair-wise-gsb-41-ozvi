"""定损标的、共保份额与结算版本的 SQLite 存取层。

仅负责 SQL 读写，不含业务规则；所有方法接收外部连接，
由服务层在同一事务里组合调用，保证与案件状态变更一起提交。
"""
from __future__ import annotations

import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS loss_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    name TEXT NOT NULL,
    sum_insured REAL NOT NULL,
    loss_ratio REAL NOT NULL,
    salvage REAL NOT NULL DEFAULT 0,
    deductible REAL NOT NULL DEFAULT 0,
    reviewed INTEGER NOT NULL DEFAULT 0,
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(claim_id,name)
);
CREATE TABLE IF NOT EXISTS coinsurers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    name TEXT NOT NULL,
    share_pct REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(claim_id,name)
);
CREATE TABLE IF NOT EXISTS settlement_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    version_no INTEGER NOT NULL,
    total_payout REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    UNIQUE(claim_id,version_no)
);
CREATE TABLE IF NOT EXISTS settlement_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES settlement_versions(id),
    item_name TEXT NOT NULL,
    sum_insured REAL NOT NULL,
    loss_ratio REAL NOT NULL,
    salvage REAL NOT NULL,
    deductible REAL NOT NULL,
    item_payout REAL NOT NULL,
    coinsurer TEXT NOT NULL,
    share_pct REAL NOT NULL,
    amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loss_items_claim ON loss_items(claim_id);
CREATE INDEX IF NOT EXISTS idx_coinsurers_claim ON coinsurers(claim_id);
CREATE INDEX IF NOT EXISTS idx_settlement_lines_version ON settlement_lines(version_id);
"""


class SettlementStore:
    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    # ---- 受损标的 ----
    def list_items(self, conn: sqlite3.Connection, claim_id: int) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM loss_items WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_item(self, conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM loss_items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def upsert_item(self, conn: sqlite3.Connection, claim_id: int, name: str, sum_insured: float,
                    loss_ratio: float, salvage: float, deductible: float, actor: str, now: str) -> tuple[int, bool]:
        """按 (claim_id, name) 幂等写入；更新已有标的后复核状态重置。"""
        existing = conn.execute(
            "SELECT id FROM loss_items WHERE claim_id=? AND name=?", (claim_id, name)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE loss_items SET sum_insured=?,loss_ratio=?,salvage=?,deductible=?,
                   reviewed=0,reviewed_by=NULL,reviewed_at=NULL,updated_at=? WHERE id=?""",
                (sum_insured, loss_ratio, salvage, deductible, now, existing["id"]),
            )
            return existing["id"], False
        cur = conn.execute(
            """INSERT INTO loss_items(claim_id,name,sum_insured,loss_ratio,salvage,deductible,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (claim_id, name, sum_insured, loss_ratio, salvage, deductible, actor, now, now),
        )
        return cur.lastrowid, True

    def delete_item(self, conn: sqlite3.Connection, item_id: int) -> None:
        conn.execute("DELETE FROM loss_items WHERE id=?", (item_id,))

    def mark_reviewed(self, conn: sqlite3.Connection, item_id: int, actor: str, now: str) -> None:
        conn.execute(
            "UPDATE loss_items SET reviewed=1,reviewed_by=?,reviewed_at=? WHERE id=?",
            (actor, now, item_id),
        )

    def has_items(self, conn: sqlite3.Connection, claim_id: int) -> bool:
        return conn.execute("SELECT 1 FROM loss_items WHERE claim_id=? LIMIT 1", (claim_id,)).fetchone() is not None

    # ---- 共保份额 ----
    def list_coinsurers(self, conn: sqlite3.Connection, claim_id: int) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM coinsurers WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
        return [dict(r) for r in rows]

    def replace_coinsurers(self, conn: sqlite3.Connection, claim_id: int,
                           coinsurers: list[dict[str, Any]], now: str) -> None:
        conn.execute("DELETE FROM coinsurers WHERE claim_id=?", (claim_id,))
        for c in coinsurers:
            conn.execute(
                "INSERT INTO coinsurers(claim_id,name,share_pct,created_at) VALUES(?,?,?,?)",
                (claim_id, c["name"], c["share_pct"], now),
            )

    # ---- 结算版本（核定后冻结，旧版本保留可查） ----
    def next_version_no(self, conn: sqlite3.Connection, claim_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM settlement_versions WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        return row["n"]

    def create_version(self, conn: sqlite3.Connection, claim_id: int, version_no: int,
                       total_payout: float, actor: str, note: str, now: str) -> int:
        cur = conn.execute(
            """INSERT INTO settlement_versions(claim_id,version_no,total_payout,note,approved_by,approved_at)
               VALUES(?,?,?,?,?,?)""",
            (claim_id, version_no, total_payout, note, actor, now),
        )
        return cur.lastrowid

    def insert_lines(self, conn: sqlite3.Connection, version_id: int, lines: list[dict[str, Any]]) -> None:
        for line in lines:
            conn.execute(
                """INSERT INTO settlement_lines(version_id,item_name,sum_insured,loss_ratio,salvage,deductible,
                   item_payout,coinsurer,share_pct,amount) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (version_id, line["item_name"], line["sum_insured"], line["loss_ratio"], line["salvage"],
                 line["deductible"], line["item_payout"], line["coinsurer"], line["share_pct"], line["amount"]),
            )

    def list_versions(self, conn: sqlite3.Connection, claim_id: int) -> list[dict[str, Any]]:
        versions = [dict(r) for r in conn.execute(
            "SELECT * FROM settlement_versions WHERE claim_id=? ORDER BY version_no DESC", (claim_id,)
        ).fetchall()]
        for v in versions:
            v["lines"] = [dict(r) for r in conn.execute(
                "SELECT * FROM settlement_lines WHERE version_id=? ORDER BY id", (v["id"],)
            ).fetchall()]
        return versions
