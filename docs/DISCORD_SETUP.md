# Discord setup (one time, ~5 minutes)

## How the bot gets messages (no polling)

The bot keeps one connection open to Discord's **Gateway** (a WebSocket). Whenever someone posts,
edits or deletes a message in a channel the bot can see, Discord pushes a `MESSAGE_CREATE` /
`MESSAGE_UPDATE` / `MESSAGE_DELETE` event to the bot instantly. Nothing is scanned periodically.

Two rules follow from this:

1. **The bot must be a member of the source server** — bots can only receive events from servers
   they have been added to. It must also be in the destination server to post there.
2. **The bot needs the _Message Content_ intent.** Without it, Discord delivers messages with empty
   text. It is a "privileged" intent: for bots in fewer than 100 servers you just switch it on.

### If you can't add a bot to the source server

If the source is an **Announcement channel** in a server you don't control, use Discord's built-in
**Follow** button on that channel and send it into a channel in *your* server (e.g. `#incoming`).
Then make a relay with `#incoming` as the source. Note: followed posts arrive as a webhook named
after the source server, so the *person* filter can't tell individual announcers apart — use the
keyword filters instead, or tick **Also relay bots & webhooks**.

(Using a personal account as a "self-bot" to read servers is against Discord's Terms of Service —
this app does not do that.)

## 1. Create the application and bot

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it
   (e.g. "Announcement Relay") and accept the terms.
2. Open the **Bot** page in the left menu.
3. Click **Reset Token**, confirm, and **copy the token**. Keep it secret — anyone with it controls
   your bot. You'll paste it into the web UI (or the `DISCORD_TOKEN` variable).
4. Still on the **Bot** page, under **Privileged Gateway Intents**, turn on
   **Message Content Intent** and click **Save Changes**. (Presence and Server Members are *not*
   needed.)
5. Optional: turn **Public Bot** off so only you can invite it.

## 2. Invite the bot to your servers

Once the app is running and connected, the **Dashboard** shows an invite link with exactly the
permissions it needs. Open it, pick a server, and authorise. Repeat for every source and destination
server. You need *Manage Server* permission in a server to add a bot.

Permissions requested and why:

| Permission | Used for |
|---|---|
| View Channels, Read Message History | Seeing source channels; "Pick from recent posters" |
| Send Messages (+ in Threads), Embed Links, Attach Files | Posting relayed messages in *Card* and *Plain* modes, re-uploading images/files |
| Manage Webhooks | *Mirror* mode — posts with the original author's name and avatar |
| Manage Messages | *Auto-publish* in destination Announcement channels |
| Mention @everyone/@here/All Roles | Only so the optional **Ping a role** works for roles that aren't normally mentionable. Relayed text never pings anyone. |

If a server uses channel-specific permission overrides, make sure the bot's role is allowed in the
channels you use. The relay editor shows a ⚠ warning when a permission is missing.

## 3. Finding IDs (only needed if you paste IDs manually)

The web UI gives you dropdowns for channels and roles and a "Pick from recent posters" button for
people, so you rarely need raw IDs. If you do: Discord → **User Settings → Advanced → Developer
Mode** on, then right-click a user/channel/role → **Copy ID**.
