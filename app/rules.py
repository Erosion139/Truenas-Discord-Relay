"""Relay rule schema, validation and filter evaluation.

Discord IDs (snowflakes) are always stored as strings so the browser's
JavaScript never rounds them (they exceed Number.MAX_SAFE_INTEGER).
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Iterable

SNOWFLAKE_RE = re.compile(r"^\d{15,21}$")
MODES = ("webhook", "embed", "plain")

DEFAULT_RULE: dict[str, Any] = {
    "name": "New relay",
    "enabled": True,
    # where to listen / where to post
    "source_channel_ids": [],
    "destination_channel_ids": [],
    # filters (empty author lists = anyone may trigger the relay)
    "author_user_ids": [],
    "author_role_ids": [],
    "include_bots": False,
    "keywords_include": [],
    "keywords_exclude": [],
    # output
    "mode": "webhook",
    "show_source_in_name": True,
    "prefix_text": "",
    "ping_role_id": "",
    "clean_mentions": True,
    "copy_attachments": True,
    "sync_edits": True,
    "sync_deletes": True,
    "auto_publish": False,
    # display names for user IDs picked in the UI (cosmetic only)
    "labels": {},
}


class RuleError(ValueError):
    pass


def _id_list(value: Any, field: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, (list, tuple)):
        raise RuleError(f"{field} must be a list of IDs")
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if not s:
            continue
        # accept mention syntax pasted from Discord: <@123>, <@!123>, <@&123>, <#123>
        s = re.sub(r"^<[@#][!&]?(\d+)>$", r"\1", s)
        if not SNOWFLAKE_RE.match(s):
            raise RuleError(f"'{s}' in {field} is not a valid Discord ID")
        if s not in out:
            out.append(s)
    return out


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split("\n")
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_rule(data: dict[str, Any], existing_id: str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuleError("Rule must be an object")
    rule = dict(DEFAULT_RULE)
    for key in DEFAULT_RULE:
        if key in data:
            rule[key] = data[key]

    rule["id"] = existing_id or str(data.get("id") or uuid.uuid4().hex[:10])
    rule["name"] = str(rule["name"]).strip()[:100] or "Unnamed relay"
    for key in ("enabled", "include_bots", "show_source_in_name", "clean_mentions",
                "copy_attachments", "sync_edits", "sync_deletes", "auto_publish"):
        rule[key] = bool(rule[key])
    for key in ("source_channel_ids", "destination_channel_ids", "author_user_ids", "author_role_ids"):
        rule[key] = _id_list(rule[key], key)
    rule["keywords_include"] = _str_list(rule["keywords_include"])
    rule["keywords_exclude"] = _str_list(rule["keywords_exclude"])
    if rule["mode"] not in MODES:
        raise RuleError(f"mode must be one of {', '.join(MODES)}")
    rule["prefix_text"] = str(rule["prefix_text"] or "")[:500]
    labels = rule["labels"] if isinstance(rule["labels"], dict) else {}
    rule["labels"] = {str(k): str(v)[:100] for k, v in labels.items()
                      if str(k) in rule["author_user_ids"]}
    ping = _id_list(rule["ping_role_id"], "ping_role_id")
    rule["ping_role_id"] = ping[0] if ping else ""

    if not rule["source_channel_ids"]:
        raise RuleError("Pick at least one source channel")
    if not rule["destination_channel_ids"]:
        raise RuleError("Pick at least one destination channel")
    overlap = set(rule["source_channel_ids"]) & set(rule["destination_channel_ids"])
    if overlap:
        raise RuleError("A channel cannot be both a source and a destination of the same rule")
    return rule


def searchable_text(content: str, embeds: Iterable[Any] = ()) -> str:
    """Text used for keyword matching: message content plus embed text."""
    parts = [content or ""]
    for e in embeds:
        for attr in ("title", "description"):
            v = getattr(e, attr, None)
            if v:
                parts.append(str(v))
        for f in getattr(e, "fields", []) or []:
            parts.append(f"{getattr(f, 'name', '')} {getattr(f, 'value', '')}")
    return "\n".join(parts).lower()


def evaluate_filters(rule: dict[str, Any], *, author_id: int | str, role_ids: Iterable[int | str],
                     is_bot: bool, text: str) -> tuple[bool, str]:
    """Return (should_relay, reason)."""
    users = set(rule.get("author_user_ids") or [])
    roles = set(rule.get("author_role_ids") or [])
    if users or roles:
        author_ok = str(author_id) in users or bool(roles & {str(r) for r in role_ids})
        if not author_ok:
            return False, "author not in filter"
    elif is_bot and not rule.get("include_bots"):
        return False, "author is a bot/webhook (enable 'Include bots')"

    text = (text or "").lower()
    include = [k.lower() for k in rule.get("keywords_include") or []]
    exclude = [k.lower() for k in rule.get("keywords_exclude") or []]
    if include and not any(k in text for k in include):
        return False, "no required keyword"
    hit = next((k for k in exclude if k in text), None)
    if hit:
        return False, f"excluded keyword '{hit}'"
    return True, "ok"


def split_text(text: str, limit: int = 2000) -> list[str]:
    """Split text into Discord-sized chunks, preferring newline then space boundaries."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks


_FORBIDDEN_NAME = re.compile(r"(discord|clyde)", re.IGNORECASE)


def safe_webhook_name(name: str) -> str:
    """Webhook usernames may not contain 'discord'/'clyde' and must be 1-80 chars."""
    def _swap(m: re.Match) -> str:
        word = m.group(0)
        # swap one letter for a lookalike: disc0rd / clyd3
        return word[:4] + "0" + word[5:] if word.lower() == "discord" else word[:4] + "3"

    name = _FORBIDDEN_NAME.sub(_swap, name or "")
    name = name.strip()[:80]
    return name or "Relay"
