"""The Discord bot: listens to gateway events and relays matching messages."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import io
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import discord

from .rules import evaluate_filters, safe_webhook_name, searchable_text, split_text
from .storage import Store

log = logging.getLogger("relay.bot")

WEBHOOK_NAME = "Announcement Relay"
MAX_DOWNLOAD = 100 * 1024 * 1024  # never pull more than this per attachment into memory
RELAYABLE_TYPES = {discord.MessageType.default, discord.MessageType.reply}

# Permissions the invite link asks for.
INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True,
    attach_files=True, read_message_history=True, manage_webhooks=True, manage_messages=True,
    mention_everyone=True,
)


class ActivityLog:
    """Ring buffer of recent relay events shown in the web UI."""

    def __init__(self, size: int = 300):
        self._items: collections.deque[dict[str, Any]] = collections.deque(maxlen=size)
        self._seq = 0

    def add(self, level: str, message: str, **extra: Any) -> None:
        self._seq += 1
        self._items.appendleft({"id": self._seq, "ts": time.time(), "level": level, "message": message, **extra})
        getattr(log, "warning" if level == "error" else "info")(message)

    def list(self, limit: int = 200, include_filtered: bool = True) -> list[dict[str, Any]]:
        items = [i for i in self._items if include_filtered or i["level"] != "filtered"]
        return items[:limit]


@dataclass
class SourceContent:
    text: str
    embeds: list[discord.Embed]
    attachments: list[discord.Attachment]
    extra_links: list[str] = field(default_factory=list)


@dataclass
class Rendered:
    chunks: list[str]                    # message bodies, each <= 2000 chars
    embeds: list[discord.Embed]          # attached to the last chunk
    username: str | None = None
    avatar_url: str | None = None

    def digest(self) -> str:
        raw = json.dumps([self.chunks, [e.to_dict() for e in self.embeds]], sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()


def extract_source(message: discord.Message, clean: bool) -> SourceContent:
    text = message.clean_content if clean else message.content
    embeds = [e for e in message.embeds if e.type == "rich"]
    attachments = list(message.attachments)
    # Discord "Forward" messages carry their payload in snapshots
    snapshots = getattr(message, "message_snapshots", None) or []
    if not text and snapshots:
        snap = snapshots[0]
        text = getattr(snap, "content", "") or ""
        embeds = [e for e in (getattr(snap, "embeds", []) or []) if e.type == "rich"]
        attachments = list(getattr(snap, "attachments", []) or [])
    links = [s.url for s in getattr(message, "stickers", []) or []]
    return SourceContent(text=text or "", embeds=embeds[:10], attachments=attachments, extra_links=links)


def render(rule: dict[str, Any], message: discord.Message, src: SourceContent, *, test: bool = False) -> Rendered:
    mode = rule.get("mode", "webhook")
    header: list[str] = []
    if rule.get("ping_role_id"):
        header.append(f"<@&{rule['ping_role_id']}>")
    if rule.get("prefix_text"):
        header.append(rule["prefix_text"])
    links = list(src.extra_links)
    author = message.author
    guild_name = message.guild.name if message.guild else "DM"
    channel_name = getattr(message.channel, "name", "channel")

    if mode == "embed":
        emb = discord.Embed(description=(src.text or None) and src.text[:4096],
                            timestamp=message.created_at, color=0x5865F2)
        emb.set_author(name=author.display_name, icon_url=author.display_avatar.url, url=message.jump_url)
        emb.set_footer(text=f"{guild_name} • #{channel_name}")
        body = "\n".join(header + links)
        return Rendered(chunks=split_text(body) if body else [], embeds=[emb] + src.embeds[:9])

    if mode == "plain":
        header.append(f"**{author.display_name}** · {guild_name} #{channel_name}")
        body = "\n".join(header + ([src.text] if src.text else []) + links)
        return Rendered(chunks=split_text(body), embeds=src.embeds)

    # webhook mode: look like the original poster
    name = author.display_name
    if rule.get("show_source_in_name", True):
        name = f"{name} • {guild_name}"
    body = "\n".join(header + ([src.text] if src.text else []) + links)
    return Rendered(chunks=split_text(body), embeds=src.embeds,
                    username=safe_webhook_name(name), avatar_url=author.display_avatar.url)


def allowed_mentions_for(rule: dict[str, Any]) -> discord.AllowedMentions:
    """Never ping anyone except the rule's explicitly configured ping role."""
    roles: Any = [discord.Object(int(rule["ping_role_id"]))] if rule.get("ping_role_id") else False
    return discord.AllowedMentions(everyone=False, users=False, roles=roles, replied_user=False)


class RelayClient(discord.Client):
    def __init__(self, store: Store, activity: ActivityLog):
        intents = discord.Intents.default()
        intents.message_content = True   # privileged: enable in the Developer Portal
        intents.members = False
        intents.presences = False
        super().__init__(intents=intents, max_messages=1000, chunk_guilds_at_startup=False)
        self.store = store
        self.feed = activity
        self._webhooks: dict[int, discord.Webhook] = {}
        self._webhook_locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self.ready_at: float | None = None

    # ------------------------------------------------------------------ events
    async def on_ready(self) -> None:
        self.ready_at = time.time()
        self.store.prune_map()
        self.feed.add("info", f"Connected as {self.user} — in {len(self.guilds)} server(s)")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.feed.add("info", f"Joined server '{guild.name}'")

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.feed.add("info", f"Removed from server '{guild.name}'")

    def _is_own(self, message: discord.Message) -> bool:
        if self.user and message.author.id == self.user.id:
            return True
        return bool(message.webhook_id and message.webhook_id in self.store.own_webhook_ids())

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or self._is_own(message):
            return
        if message.type not in RELAYABLE_TYPES:
            return
        rules = self.store.rules_for_source(message.channel.id)
        if not rules:
            return
        author = message.author
        role_ids = [r.id for r in getattr(author, "roles", [])]
        is_bot = author.bot or message.webhook_id is not None
        for rule in rules:
            src = extract_source(message, rule.get("clean_mentions", True))
            ok, reason = evaluate_filters(rule, author_id=author.id, role_ids=role_ids, is_bot=is_bot,
                                          text=searchable_text(src.text, src.embeds))
            where = f"{message.guild.name} #{getattr(message.channel, 'name', '?')}"
            if not ok:
                self.feed.add("filtered", f"[{rule['name']}] skipped message by {author} in {where}: {reason}",
                                  rule_id=rule["id"])
                continue
            try:
                sent = await self.relay(rule, message, src)
                self.feed.add("relayed", f"[{rule['name']}] relayed message by {author} from {where} "
                                             f"to {sent} channel(s)", rule_id=rule["id"], jump_url=message.jump_url)
            except Exception as exc:  # keep the bot alive no matter what
                log.exception("relay failed")
                self.feed.add("error", f"[{rule['name']}] relay failed: {exc}", rule_id=rule["id"])

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        rows = self.store.get_map(payload.message_id)
        if not rows:
            return
        message = getattr(payload, "message", None) or payload.cached_message
        if message is None:
            return
        for row in rows:
            rule = self.store.get_rule(row["rule_id"])
            if not rule or not rule.get("sync_edits"):
                continue
            try:
                await self._apply_edit(rule, message, row)
            except Exception as exc:
                self.feed.add("error", f"[{rule['name']}] could not sync edit: {exc}", rule_id=rule["id"])

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        rows = self.store.get_map(payload.message_id)
        if not rows:
            return
        for row in rows:
            rule = self.store.get_rule(row["rule_id"])
            if not rule or not rule.get("sync_deletes"):
                continue
            try:
                channel = await self._resolve_channel(int(row["dest_channel_id"]))
                for mid in row["dest_message_ids"]:
                    if row["webhook_id"]:
                        hook = await self._get_webhook(channel)
                        kw = {"thread": discord.Object(int(row["thread_id"]))} if row["thread_id"] else {}
                        await hook.delete_message(int(mid), **kw)
                    else:
                        await channel.get_partial_message(int(mid)).delete()
                self.feed.add("relayed", f"[{rule['name']}] deleted relayed copy (source was deleted)",
                                  rule_id=rule["id"])
            except discord.NotFound:
                pass
            except Exception as exc:
                self.feed.add("error", f"[{rule['name']}] could not sync delete: {exc}", rule_id=rule["id"])
        self.store.delete_map(payload.message_id)

    # ------------------------------------------------------------------ helpers
    async def _resolve_channel(self, channel_id: int) -> Any:
        ch = self.get_channel(channel_id)
        if ch is None:
            ch = await self.fetch_channel(channel_id)
        return ch

    async def _get_webhook(self, channel: Any) -> discord.Webhook:
        target = channel.parent if isinstance(channel, discord.Thread) else channel
        if target.id in self._webhooks:
            return self._webhooks[target.id]
        async with self._webhook_locks[target.id]:
            if target.id in self._webhooks:
                return self._webhooks[target.id]
            hook = None
            for h in await target.webhooks():
                if h.user and self.user and h.user.id == self.user.id and h.token:
                    hook = h
                    break
            if hook is None:
                hook = await target.create_webhook(name=WEBHOOK_NAME, reason="Announcement relay destination")
            self.store.add_own_webhook(hook.id, target.id)
            self._webhooks[target.id] = hook
            return hook

    async def _download(self, attachments: list[discord.Attachment]) -> list[tuple[discord.Attachment, bytes | None]]:
        out = []
        for a in attachments:
            data = None
            if a.size <= MAX_DOWNLOAD:
                try:
                    data = await a.read()
                except Exception as exc:
                    log.warning("could not download %s: %s", a.filename, exc)
            out.append((a, data))
        return out

    async def relay(self, rule: dict[str, Any], message: discord.Message, src: SourceContent) -> int:
        downloaded = await self._download(src.attachments) if rule.get("copy_attachments", True) else []
        sent = 0
        errors = []
        for dest_id in rule["destination_channel_ids"]:
            try:
                await self._send_to(rule, message, src, int(dest_id), downloaded)
                sent += 1
            except discord.Forbidden as exc:
                errors.append(f"#{dest_id}: missing permission ({exc.text})")
            except Exception as exc:
                errors.append(f"#{dest_id}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors) + (f" (sent to {sent})" if sent else ""))
        return sent

    async def _send_to(self, rule: dict[str, Any], message: discord.Message, src: SourceContent, dest_id: int,
                       downloaded: list[tuple[discord.Attachment, bytes | None]], test: bool = False) -> None:
        channel = await self._resolve_channel(dest_id)
        rendered = render(rule, message, src)
        limit = getattr(getattr(channel, "guild", None), "filesize_limit", 10 * 1024 * 1024)
        files: list[discord.File] = []
        links: list[str] = []
        for att, data in downloaded:
            if data is not None and len(data) <= limit and len(files) < 10:
                files.append(discord.File(io.BytesIO(data), filename=att.filename, spoiler=att.is_spoiler()))
            else:
                links.append(att.url)
        chunks = list(rendered.chunks)
        if links:
            chunks = split_text("\n".join(chunks + links)) if chunks else split_text("\n".join(links))
        if not chunks and not rendered.embeds and not files:
            chunks = ["*(empty message)*"]

        mentions = allowed_mentions_for(rule)
        use_webhook = rule.get("mode") == "webhook"
        thread_id = channel.id if isinstance(channel, discord.Thread) else None
        sent_ids: list[int] = []
        webhook = await self._get_webhook(channel) if use_webhook else None
        total = max(len(chunks), 1)
        for i in range(total):
            last = i == total - 1
            kwargs: dict[str, Any] = {"allowed_mentions": mentions}
            if i < len(chunks):
                kwargs["content"] = chunks[i]
            if last:
                if rendered.embeds:
                    kwargs["embeds"] = rendered.embeds
                if files:
                    kwargs["files"] = files
            if webhook is not None:
                kwargs.update(username=rendered.username, avatar_url=rendered.avatar_url, wait=True)
                if thread_id:
                    kwargs["thread"] = discord.Object(thread_id)
                try:
                    msg = await webhook.send(**kwargs)
                except discord.NotFound:
                    # someone deleted our webhook: forget it, recreate once, retry
                    target = channel.parent if isinstance(channel, discord.Thread) else channel
                    self._webhooks.pop(target.id, None)
                    webhook = await self._get_webhook(channel)
                    if "files" in kwargs:  # sent File objects are closed; rebuild them
                        kwargs["files"] = [discord.File(io.BytesIO(d), filename=a.filename, spoiler=a.is_spoiler())
                                           for a, d in downloaded
                                           if d is not None and len(d) <= limit][:10]
                    msg = await webhook.send(**kwargs)
            else:
                msg = await channel.send(**kwargs)
            sent_ids.append(msg.id)
            if rule.get("auto_publish") and getattr(channel, "is_news", lambda: False)():
                try:
                    await channel.get_partial_message(msg.id).publish()
                except discord.HTTPException as exc:
                    self.feed.add("error", f"[{rule['name']}] could not publish in #{channel}: {exc}")

        if not test:
            self.store.add_map(message.id, rule["id"], dest_id, sent_ids,
                               webhook.id if webhook else None, thread_id, rendered.digest())

    async def _apply_edit(self, rule: dict[str, Any], message: discord.Message, row: dict[str, Any]) -> None:
        src = extract_source(message, rule.get("clean_mentions", True))
        rendered = render(rule, message, src)
        digest = rendered.digest()
        if digest == row["content_hash"]:
            return  # e.g. Discord only resolved a link preview
        channel = await self._resolve_channel(int(row["dest_channel_id"]))
        ids = row["dest_message_ids"]
        chunks = list(rendered.chunks)
        # fit the new text into the number of messages we already posted
        if len(chunks) > len(ids):
            tail = "\n".join(chunks[len(ids) - 1:])
            chunks = chunks[:len(ids) - 1] + [tail[:1997] + "…" if len(tail) > 2000 else tail]
        mentions = allowed_mentions_for(rule)
        for i, mid in enumerate(ids):
            last = i == len(ids) - 1
            content = chunks[i] if i < len(chunks) else "​"
            embeds = rendered.embeds if last else []
            if row["webhook_id"]:
                hook = await self._get_webhook(channel)
                kw = {"thread": discord.Object(int(row["thread_id"]))} if row["thread_id"] else {}
                await hook.edit_message(int(mid), content=content, embeds=embeds, allowed_mentions=mentions, **kw)
            else:
                await channel.get_partial_message(int(mid)).edit(content=content, embeds=embeds,
                                                                 allowed_mentions=mentions)
        self.store.update_map(row["rowid"], ids, digest)
        self.feed.add("relayed", f"[{rule['name']}] synced an edit", rule_id=rule["id"])

    # ------------------------------------------------------------------ web UI helpers
    async def send_test(self, rule: dict[str, Any]) -> list[dict[str, Any]]:
        results = []
        for dest_id in rule["destination_channel_ids"]:
            try:
                channel = await self._resolve_channel(int(dest_id))
                text = f"✅ Test message from relay rule **{rule['name']}**. If you can read this, the destination works."
                if rule.get("mode") == "webhook":
                    hook = await self._get_webhook(channel)
                    kw = {"thread": discord.Object(channel.id)} if isinstance(channel, discord.Thread) else {}
                    await hook.send(content=text, username=WEBHOOK_NAME, avatar_url=self.user.display_avatar.url,
                                    allowed_mentions=discord.AllowedMentions.none(), **kw)
                else:
                    await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                results.append({"channel_id": dest_id, "ok": True, "message": f"Sent to #{channel}"})
            except discord.Forbidden as exc:
                results.append({"channel_id": dest_id, "ok": False, "message": f"Missing permission: {exc.text}"})
            except Exception as exc:
                results.append({"channel_id": dest_id, "ok": False, "message": str(exc)})
        return results

    async def recent_authors(self, channel_id: int, limit: int = 100) -> list[dict[str, Any]]:
        channel = await self._resolve_channel(channel_id)
        seen: dict[int, dict[str, Any]] = {}
        async for m in channel.history(limit=limit):
            a = m.author
            if a.id not in seen:
                seen[a.id] = {"id": str(a.id), "name": str(a), "display_name": a.display_name,
                              "bot": a.bot or m.webhook_id is not None, "avatar": a.display_avatar.url,
                              "count": 0, "last_seen": m.created_at.isoformat()}
            seen[a.id]["count"] += 1
        return sorted(seen.values(), key=lambda x: -x["count"])

    def guild_snapshot(self) -> list[dict[str, Any]]:
        out = []
        for g in sorted(self.guilds, key=lambda g: g.name.lower()):
            me = g.me
            channels = []
            for ch in g.channels:
                if not isinstance(ch, discord.TextChannel):
                    continue
                p = ch.permissions_for(me) if me else discord.Permissions.none()
                channels.append({
                    "id": str(ch.id), "name": ch.name, "news": ch.is_news(),
                    "category": ch.category.name if ch.category else "", "position": ch.position,
                    "perms": {"view": p.view_channel, "read_history": p.read_message_history,
                              "send": p.send_messages, "embed": p.embed_links, "attach": p.attach_files,
                              "webhooks": p.manage_webhooks, "manage_messages": p.manage_messages},
                })
            channels.sort(key=lambda c: (c["category"].lower(), c["position"]))
            roles = [{"id": str(r.id), "name": r.name, "color": f"#{r.color.value:06x}" if r.color.value else ""}
                     for r in sorted(g.roles, key=lambda r: -r.position) if not r.is_default()]
            out.append({"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None,
                        "channels": channels, "roles": roles})
        return out


class BotManager:
    """Owns the bot lifecycle so the web UI can (re)start it with a new token."""

    def __init__(self, store: Store, activity: ActivityLog):
        self.store = store
        self.feed = activity
        self.client: RelayClient | None = None
        self._task: asyncio.Task | None = None
        self.status = "stopped"
        self.error: str | None = None

    async def start(self, token: str) -> None:
        await self.stop()
        self.status, self.error = "connecting", None
        self._task = asyncio.create_task(self._run(token))

    async def _run(self, token: str) -> None:
        delay = 5
        while True:
            client = RelayClient(self.store, self.feed)
            self.client = client
            try:
                await client.start(token, reconnect=True)
                self.status = "stopped"
                return
            except discord.LoginFailure:
                self.status, self.error = "error", ("Invalid bot token. Reset it in the Developer Portal "
                                                    "and paste the new one.")
            except discord.PrivilegedIntentsRequired:
                self.status, self.error = "error", ("The MESSAGE CONTENT intent is not enabled. Developer Portal → "
                                                    "your app → Bot → Privileged Gateway Intents → enable "
                                                    "Message Content Intent, then click Reconnect.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # network trouble (e.g. NAS booted before DNS was up): keep retrying with backoff
                self.status, self.error = "error", f"{type(exc).__name__}: {exc} — retrying in {delay}s"
                self.feed.add("error", f"Bot connection failed: {self.error}")
                try:
                    await client.close()
                except Exception:
                    pass
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
                self.status, self.error = "connecting", None
                continue
            self.feed.add("error", f"Bot stopped: {self.error}")
            return

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.client, self._task, self.status = None, None, "stopped"

    def snapshot(self) -> dict[str, Any]:
        c = self.client
        ready = bool(c and c.is_ready())
        status = "ready" if ready else self.status
        if status == "connecting" and c and c.ready_at:
            status = "reconnecting"
        data: dict[str, Any] = {"status": status, "error": self.error, "user": None, "invite_url": None,
                                "guild_count": 0, "latency_ms": None, "ready_since": None}
        if c and c.user:
            data["user"] = {"id": str(c.user.id), "name": str(c.user), "avatar": c.user.display_avatar.url}
        if c and c.application_id:
            data["invite_url"] = discord.utils.oauth_url(c.application_id, permissions=INVITE_PERMISSIONS,
                                                         scopes=("bot",))
        if ready:
            data["guild_count"] = len(c.guilds)
            data["latency_ms"] = round(c.latency * 1000) if c.latency == c.latency else None
            data["ready_since"] = c.ready_at
        return data
