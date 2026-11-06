# Build on GitHub and install on TrueNAS CE

Written against TrueNAS CE 25.10 ("Goldeye"). Earlier Docker-based releases (24.10, 25.04) use the
same **Custom App** screen with minor label differences.

---

## Part A — Put the code on GitHub and let Actions build the image

1. Create a new repository on GitHub (e.g. `discord-relay`). Public or private both work.
2. **First, rename the `_github` folder to `.github`** (GitHub only runs workflows from
   `.github/workflows/`; the folder ships as `_github` because it couldn't be written to your PC
   under its real name). In Windows Explorer just rename it to `.github` — or in a terminal:
   `ren _github .github` (cmd) / `mv _github .github`.
   Then upload everything in this folder **including `.github`**:
   - With Git:
     ```bash
     cd "Truenas CE Discord Relay/discord-relay"
     git init -b main
     git add .
     git commit -m "Discord relay"
     git remote add origin https://github.com/YOUR-USER/discord-relay.git
     git push -u origin main
     ```
   - Or on github.com: **Add file → Upload files**, drag in the folder contents. (Windows Explorer
     hides `.github` by default — check that `.github/workflows/docker-publish.yml` made it into
     the repo, otherwise create it via **Add file → Create new file** and paste the contents.)
3. Open the repo's **Actions** tab. The **Build & publish Docker image** workflow runs the tests,
   then builds for `amd64` + `arm64` and pushes to GitHub Container Registry. It takes ~3–5 minutes.
   If Actions is disabled, enable it on that tab first.
4. When it finishes, your image is at:
   ```
   ghcr.io/YOUR-USER/discord-relay:latest
   ```
   (always **lowercase**, even if your username has capitals). You'll find it under your GitHub
   profile → **Packages**.
5. **Make the package public** so TrueNAS can pull it without a login: Packages → `discord-relay`
   → **Package settings** → *Danger Zone* → **Change visibility → Public**. The image contains no
   secrets — your token and password live only on your NAS.

Every later push to `main` rebuilds `:latest`. Pushing a tag like `v1.2.0` also publishes
`:1.2.0` and `:1.2` if you prefer pinned versions.

---

## Part B — Install on TrueNAS with the Custom App screen

1. In the TrueNAS web UI open **Apps**. (First time only: TrueNAS asks you to choose a pool for
   apps — pick one.)
2. Click **Discover Apps**, then **Custom App** (top right). The *Install iX App* form opens.
3. Fill it in — leave anything not listed at its default:

| Section | Field | Value |
|---|---|---|
| Application Name | Application Name | `discord-relay` |
| Image Configuration | Repository | `ghcr.io/your-user/discord-relay` |
| | Tag | `latest` |
| | Pull Policy | *Always pull image even if present on host* (so a restart picks up new builds) |
| Container Configuration | Timezone | your timezone (affects log timestamps) |
| | Environment Variables | *optional* — see below |
| | Restart Policy | *Unless Stopped* |
| Security Context | Custom User | **leave unticked** (the container starts as root only to fix `/data` ownership, then drops to user/group 568 = TrueNAS `apps`) |
| Network Configuration | Ports → **Add** | Container Port `8080`, Host Port `8085`, Protocol `TCP` |
| Portal Configuration | Name / Protocol / Port / Path | `Web UI` / `HTTP` / `8085` / `/` (adds a **Web UI** button to the app card) |
| Storage Configuration | Storage → **Add** | Type **ixVolume**, Dataset Name `data`, Mount Path `/data` (leave *Enable ACL* off) |
| Resources | | defaults are plenty (the app uses ~60–100 MB RAM) |

   *Host port:* any free port works; `8085` is just a suggestion. Don't use ports TrueNAS itself
   uses (80/443).

   *Storage alternative:* if you want the data in a dataset you can browse/back up, create a dataset
   (e.g. `tank/apps/discord-relay`), then choose Type **Host Path**, Host Path
   `/mnt/tank/apps/discord-relay`, Mount Path `/data`.

   **Optional environment variables** (Container Configuration → Environment Variables → Add):

   | Name | Value | Effect |
   |---|---|---|
   | `DISCORD_TOKEN` | your bot token | Token comes from TrueNAS instead of the web UI |
   | `WEB_PASSWORD` | a password | Fixed web UI password (otherwise you create one on first visit) |
   | `LOG_LEVEL` | `DEBUG` | More detailed container logs |

4. Click **Install**. The app shows *Deploying*, then *Running* within a minute.
5. Click **Web UI** on the app card (or browse to `http://<truenas-ip>:8085`).
6. First visit: create the web UI password → **Settings** → paste the bot token →
   **Save & connect**. The status pill turns green (**Online**).
7. **Dashboard** → copy the invite link → add the bot to your source and destination servers →
   **Refresh**.
8. **Relays** → **New relay**: pick the source channel, add the people/roles to listen to
   ("Pick from recent posters" lists who has posted there lately), pick destinations, choose a
   style, **Save**. Use **Send test** to confirm the bot can post to the destination.

### Install via YAML instead (optional)

**Apps → Discover Apps → ⋮ (three dots) → Install via YAML**, name it `discord-relay`, and paste
`docker-compose.yml` from this repo after changing the image name and replacing `./data` with a
dataset path such as `/mnt/tank/apps/discord-relay`.

---

## Updating

After pushing changes and waiting for the Action to finish: **Apps → discord-relay → Stop**, then
**Start** (with *Always pull* set, TrueNAS fetches the new `latest`). Your relays, token and
password are kept in `/data`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App stuck *Deploying*, log says `denied` / `unauthorized` pulling the image | The GHCR package is private — make it public (Part A step 5) — or the Repository name has capitals. |
| Status shows *Invalid bot token* | Reset the token in the Developer Portal and paste the new one. |
| Status mentions *MESSAGE CONTENT intent* | Developer Portal → Bot → enable **Message Content Intent** → Save, then **Settings → Reconnect bot**. |
| Relay saved but nothing happens | Check **Activity** with *Show filtered* on: it tells you if the author/keyword filter skipped the message. Make sure the bot can see the source channel. |
| *Send test* says missing permission | Give the bot's role the listed permission in that channel (Mirror mode needs **Manage Webhooks**). |
| `/data is not writable` in the logs | You enabled *Custom User*; untick it, or give UID 568 write access to the dataset. |
| Container logs | **Apps → discord-relay → Logs** (the icon on the Workloads/container row). |
