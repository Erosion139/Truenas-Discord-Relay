"""SQLite persistence: settings, relay rules, our webhooks, and source->relayed message map."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, position INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS own_webhooks (id TEXT PRIMARY KEY, channel_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS message_map (
    source_message_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    dest_channel_id TEXT NOT NULL,
    dest_message_ids TEXT NOT NULL,
    webhook_id TEXT,
    thread_id TEXT,
    content_hash TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_map_source ON message_map(source_message_id);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
        self._rules_cache: list[dict[str, Any]] | None = None
        self._own_hooks: set[int] | None = None

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self._q("DELETE FROM settings WHERE key=?", (key,))
        else:
            self._q("INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---- rules ----------------------------------------------------------
    def list_rules(self) -> list[dict[str, Any]]:
        if self._rules_cache is None:
            rows = self._q("SELECT data FROM rules ORDER BY position, rowid")
            self._rules_cache = [json.loads(r["data"]) for r in rows]
        return [dict(r) for r in self._rules_cache]

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        return next((r for r in self.list_rules() if r["id"] == rule_id), None)

    def save_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        pos = len(self.list_rules())
        self._q("INSERT INTO rules(id,position,data) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data", (rule["id"], pos, json.dumps(rule)))
        self._rules_cache = None
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        existed = self.get_rule(rule_id) is not None
        self._q("DELETE FROM rules WHERE id=?", (rule_id,))
        self._q("DELETE FROM message_map WHERE rule_id=?", (rule_id,))
        self._rules_cache = None
        return existed

    def rules_for_source(self, channel_id: int) -> list[dict[str, Any]]:
        cid = str(channel_id)
        return [r for r in self.list_rules() if r.get("enabled") and cid in r.get("source_channel_ids", [])]

    # ---- our webhooks (loop protection) --------------------------------
    def own_webhook_ids(self) -> set[int]:
        if self._own_hooks is None:
            self._own_hooks = {int(r["id"]) for r in self._q("SELECT id FROM own_webhooks")}
        return self._own_hooks

    def add_own_webhook(self, webhook_id: int, channel_id: int) -> None:
        self._q("INSERT OR IGNORE INTO own_webhooks(id,channel_id) VALUES(?,?)", (str(webhook_id), str(channel_id)))
        self.own_webhook_ids().add(int(webhook_id))

    # ---- message map (edit / delete sync) ------------------------------
    def add_map(self, source_message_id: int, rule_id: str, dest_channel_id: int, dest_ids: list[int],
                webhook_id: int | None, thread_id: int | None, content_hash: str) -> None:
        self._q("INSERT INTO message_map VALUES(?,?,?,?,?,?,?,?)", (
            str(source_message_id), rule_id, str(dest_channel_id), json.dumps([str(i) for i in dest_ids]),
            str(webhook_id) if webhook_id else None, str(thread_id) if thread_id else None,
            content_hash, time.time()))

    def get_map(self, source_message_id: int) -> list[dict[str, Any]]:
        rows = self._q("SELECT rowid, * FROM message_map WHERE source_message_id=?", (str(source_message_id),))
        out = []
        for r in rows:
            d = dict(r)
            d["dest_message_ids"] = json.loads(d["dest_message_ids"])
            out.append(d)
        return out

    def update_map(self, rowid: int, dest_ids: list[int | str], content_hash: str) -> None:
        self._q("UPDATE message_map SET dest_message_ids=?, content_hash=? WHERE rowid=?",
                (json.dumps([str(i) for i in dest_ids]), content_hash, rowid))

    def delete_map(self, source_message_id: int) -> None:
        self._q("DELETE FROM message_map WHERE source_message_id=?", (str(source_message_id),))

    def prune_map(self, days: int = 30) -> None:
        self._q("DELETE FROM message_map WHERE created_at < ?", (time.time() - days * 86400,))
