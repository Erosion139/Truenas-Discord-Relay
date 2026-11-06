# Discord Relay

A small self-hosted Discord bot with a web UI that copies messages from one channel to others —
across servers, standard or Announcement channels — with filters so only the people (or roles)
you care about get relayed. Built to run as a Docker app on TrueNAS CE.

- **Instant, no polling** — listens to Discord Gateway events (`MESSAGE_CREATE/UPDATE/DELETE`).
- **Filters** — specific people, specific roles, include/exclude keywords, bots on/off.
- **Many-to-many** — each relay can have several sources and several destinations; make as many relays as you like.
- **Three styles** — *Mirror* (webhook with the original poster's name & avatar), *Card* (embed with link back), *Plain*.
- **Keeps copies in sync** — edits and deletions of the original are applied to the copies.
- **Attachments** re-uploaded (falls back to links when too large), forwarded messages and long (>2000 char) posts handled.
- **Safe pings** — relayed text never pings @everyone/users/roles; optionally ping one role of your choosing in the destination.
- **Auto-publish** into destination Announcement channels.
- **Web UI** — password-protected; channel/role pickers, "pick from recent posters", test button, live activity log with filter reasons.

## Quick start

1. [Discord setup](docs/DISCORD_SETUP.md) — create the bot, copy its token, enable *Message Content Intent*.
2. Rename the `_github` folder to `.github`, then follow [GitHub + TrueNAS install](docs/TRUENAS_INSTALL.md) — push this repo, let Actions build the image, add it as a Custom App.
3. Open the web UI, paste the token, invite the bot, create a relay.

Run locally without TrueNAS:

```bash
docker compose up -d          # after editing the image name in docker-compose.yml
# or, for development:
pip install -r requirements.txt
DATA_DIR=./data python -m app   # http://localhost:8080
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token. If unset, set it in the web UI (stored in `/data`). |
| `WEB_PASSWORD` | — | Web UI password. If unset, you create one on first visit. |
| `WEB_PORT` | `8080` | Port inside the container. |
| `DATA_DIR` | `/data` | SQLite database location — mount a volume here. |
| `PUID` / `PGID` | `568` | User the app runs as (568 = TrueNAS `apps`). |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail. |

## Project layout

```
app/
  main.py        starts web server + bot in one asyncio loop
  relay.py       Discord client: events, filtering, sending, edit/delete sync
  rules.py       relay rule schema, validation, filter logic
  storage.py     SQLite (settings, rules, message map)
  web.py         aiohttp JSON API + auth
  static/        web UI (plain HTML/CSS/JS, no build step)
tests/           pytest unit tests (run in CI before building)
Dockerfile, docker-entrypoint.sh, docker-compose.yml
_github/workflows/docker-publish.yml   (rename to .github) builds & pushes ghcr.io/<you>/<repo>
```

## Security notes

- The web UI is meant for your LAN. Don't port-forward it to the internet; use a VPN or a reverse proxy with HTTPS if you need remote access.
- The bot token is stored in plain text in `/data/relay.db` (or in the TrueNAS app config if you use `DISCORD_TOKEN`). Protect that dataset like any secret.
- Only relay content you're allowed to share — announcement authors may not expect their posts to be copied elsewhere.
