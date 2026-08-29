# weibo-monitor

Self-hosted Weibo user monitor. It polls Weibo's desktop Ajax API, captures Playwright screenshots, and sends notifications to Telegram.

> [!IMPORTANT]
> This is an independent, unofficial project and is not affiliated with, endorsed by, or sponsored by Weibo, Sina, or Telegram. Weibo and Telegram names and trademarks belong to their respective owners. Use the software only for public content that you are permitted to access, and comply with applicable platform terms, laws, copyright, privacy, and rate limits.

## Features

- New-post detection for original posts and reposts
- Desktop Playwright screenshots
- Video download and Telegram upload; Pyrogram handles files of at least 50 MB
- Comment monitoring with picture support
- Profile and follow-list change detection
- Revisioned cookie state shared by httpx and Playwright with stale-writer protection
- Authenticated first-party session refresh with rotated-cookie persistence
- Durable SQLite delivery outbox with idempotent event keys and bounded retry backoff
- First-run modes that archive or send existing posts up to a 50-page safety limit

## Requirements

- Docker Engine with Docker Compose v2
- A Weibo account that can access the monitored public pages
- A Telegram bot and a private chat/channel for notifications
- Optional Telegram API credentials for large-file uploads through Pyrogram

Use a dedicated, least-privileged Weibo account when possible. Do not monitor private, restricted, or otherwise unauthorized content. Keep the destination Telegram chat private if notifications may contain personal information.

## Installation

1. Clone the repository and create the local configuration:

   ```bash
   git clone https://github.com/SeaL773/weibo-monitor.git
   cd weibo-monitor
   cp .env.example .env
   ```

2. Edit `.env`:

   ```dotenv
   WEIBO_COOKIES=[{"name":"SUB","value":"...","domain":".weibo.com","path":"/","secure":true,"httpOnly":true}]
   WEIBO_TARGETS=1234567,7654321
   USER_TAGS=1234567:UserA,7654321:UserB
   TG_BOT_TOKEN=TOKEN
   TG_CHAT_ID=CHAT_ID
   POLL_INTERVAL=300
   COOKIE_REFRESH_INTERVAL=21600
   FIRST_RUN_MODE=archive
   ```

   Export the complete `weibo.com` cookie set from a browser session that you control. `WEIBO_TARGETS` is a comma-separated list of numeric Weibo UIDs. Labels are configured separately through optional `USER_TAGS` entries in `uid:label` form. Labels must not be appended to `WEIBO_TARGETS`.

3. Build and start the service:

   ```bash
   # Linux bind mounts must be writable by the container user (UID/GID 10001).
   mkdir -p data
   sudo chown -R 10001:10001 data
   docker compose up -d --build
   docker compose logs -f --tail=100
   ```

   On Docker Desktop for Windows or macOS, create `data/` normally; the Linux ownership command is typically unnecessary.

The initial image build can take several minutes while Chromium is installed. Runtime state is stored under `./data` and survives container recreation.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `TG_BOT_TOKEN` | Telegram Bot API token | required |
| `TG_BOT_TOKEN_FILE` | File containing the Telegram bot token; takes precedence over `TG_BOT_TOKEN` | optional |
| `TG_CHAT_ID` | Private Telegram chat/channel ID | required |
| `TG_API_ID` | Telegram API ID used by Pyrogram | `0` (disabled) |
| `TG_API_HASH` | Telegram API hash used by Pyrogram | optional |
| `TG_API_HASH_FILE` | File containing the Telegram API hash | optional |
| `WEIBO_COOKIES` | Initial browser cookie export; used only when persistent state is empty | required on first boot |
| `WEIBO_COOKIES_FILE` | File containing the initial cookie JSON | optional |
| `WEIBO_TARGETS` | Comma-separated Weibo UIDs, such as `1234567,7654321` | required |
| `USER_TAGS` | Optional comma-separated `uid:label` mappings | empty |
| `POLL_INTERVAL` | Base polling interval in seconds (minimum 60), with ±60-second jitter | `300` |
| `POLL_MAX_PAGES` | Maximum regular-poll pages before checkpoints stop advancing | `10` |
| `FOLLOW_CHECK_INTERVAL` | Follow-list check interval in seconds (minimum 60) | `1800` |
| `COOKIE_REFRESH_INTERVAL` | Authenticated first-party session-refresh interval in seconds (minimum 300) | `21600` |
| `HEALTH_TIMEOUT` | Internal watchdog and container health timeout in seconds (minimum 300) | `1800` |
| `OUTBOX_BATCH_SIZE` | Maximum due delivery events handled per pass (`1`–`200`) | `20` |
| `OUTBOX_FILE` | SQLite outbox path inside the container | `data/outbox.db` |
| `MAX_IMAGE_BYTES` | Maximum archived image size in bytes (minimum 1024) | `20971520` |
| `MAX_VIDEO_BYTES` | Maximum downloaded video size in bytes (minimum 1024) | `524288000` |
| `FIRST_RUN_MODE` | `archive` records existing post IDs; `push` sends existing posts | `archive` |

`*_FILE` variables refer to paths inside the container. If you use them, mount
the corresponding secret files read-only yourself. `TG_CHAT_ID` does not have a
`_FILE` variant.

Docker Compose resource limits can be adjusted with host-side interpolation:

| Variable | Description | Default |
|---|---|---|
| `MEM_LIMIT` | Container memory limit | `2g` |
| `MEM_RESERVATION` | Container memory reservation | `512m` |
| `CPU_LIMIT` | Container CPU limit | `2.0` |

The default Compose service also uses a read-only root filesystem, 512 MiB
`/tmp` tmpfs, 512 MiB shared memory, a 256-process limit, a three-minute stop
grace period, and local log rotation capped at three 10 MiB files.

## Cookie lifecycle and security

On first boot, the monitor imports `WEIBO_COOKIES` into `data/auth_state.json`. The persistent file then becomes authoritative:

- httpx saves server-side cookie rotations and updates `X-XSRF-TOKEN`.
- httpx and Playwright write against a monotonic revision. A stale client or
  browser context is rejected instead of rolling back newer rotated cookies.
- Playwright commits cookies only after an authenticated first-party probe.
- The `./data:/app/data` volume retains authentication state across rebuilds.
- Cookie values should not appear in normal application logs.

Cookies are credentials and are stored **unencrypted at rest** in `.env` and `data/auth_state.json`. Never commit, paste into issues, include in Docker images, or share these files. Restrict host access to the project directory and backups. File mode `0600` is attempted for the persistent store, but permissions on Windows bind mounts depend on Windows ACLs; protect the directory with an ACL and full-disk encryption. Rotate the Weibo session immediately if a cookie may have leaked.

`COOKIE_REFRESH_INTERVAL` only performs authenticated first-party activity and
can preserve a still-valid session; it cannot renew a server-revoked session.
The browser session on the host and the CookieStore session inside Docker are
separate. If logs show `code=-101` / `账号未登录`, log in again through the
Docker session or export a fresh complete cookie set, then replace
`data/auth_state.json` while the container is stopped and recreate it.

To replace an expired session:

```bash
docker compose stop
mv data/auth_state.json data/auth_state.json.bak
# Replace WEIBO_COOKIES in .env, then:
docker compose up -d --build --force-recreate
```

After confirming the new session works, securely delete the backup. A plain `docker restart` does not reload `.env`.

## Upgrade

Stop the container before backing up `.env` and the complete `data/` directory,
then review release notes and rebuild. The full data backup includes the SQLite
outbox and any `-wal`/`-shm` sidecars:

```bash
docker compose stop
git pull --ff-only
docker compose up -d --build --force-recreate
docker compose logs --tail=100
```

## Testing

The default unit suite is offline. It covers cookie concurrency, fetch
completeness, outbox/delivery retry, atomic state persistence, security
boundaries, and Telegram transport retry:

```bash
python -m unittest discover -v -p "test_*.py"
docker compose config
```

Any manual/live push test can contact Weibo and send a real Telegram message. Run live tests only with dedicated test accounts and an explicit live-test opt-in documented by the script; they are not part of the default test suite.

```bash
RUN_LIVE_TESTS=1 python scripts/manual_push_test.py
```

## Data, privacy, and retention

The service can retain post IDs, profile/follow metadata, comments, downloaded media, screenshots, logs, Telegram/Pyrogram session data, authentication cookies, and pending outbox payloads. Pending payloads can contain post/comment text, profile metadata, and media URLs. Operators are responsible for providing any required notice, choosing an appropriate lawful basis, minimizing collection, and honoring deletion or access requests. Content remains owned by its original rightsholders; this license does not grant rights to Weibo content or third-party media.

Delivery intent is committed to `data/outbox.db` before business checkpoints
advance. Failed events retry with exponential delays from 30 seconds up to one
hour. Delivered payloads are erased immediately while deduplication keys are
retained for 30 days. Invalid `state.json` or archive JSON is quarantined with a
`.corrupt-<timestamp>` suffix rather than silently overwritten.

Choose a retention period appropriate for your use and periodically delete unneeded files from `data/`, Docker logs, backups, and the Telegram destination. Stopping or removing a container does not delete bind-mounted data or messages already delivered to Telegram.

Versions before the unified CookieStore may have created `data/cookies_runtime.json`.
After confirming `data/auth_state.json` works, delete the obsolete runtime file.

To uninstall while preserving data:

```bash
docker compose down --remove-orphans
```

To erase local runtime data, first run the command above, then delete the project's `data/` directory and `.env` using your platform's secure deletion or recycle-bin workflow. Delete corresponding Telegram messages separately.

## Project structure

```text
weibo-monitor/
├── src/
│   ├── api/                  # Weibo clients and cookie state
│   ├── archive/              # State, archive, and SQLite outbox persistence
│   ├── core/                 # Monitoring and notification logic
│   ├── screenshot/           # Playwright screenshot capture
│   └── telegram/             # Bot API and Pyrogram transports
├── data/                     # state.json, auth_state.json, outbox.db, media
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## Security and contributing

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines. Dependency license information is summarized in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

The source code is licensed under the [MIT License](LICENSE). This license applies only to this project's source code, not to monitored content, screenshots, downloaded media, platform APIs, trademarks, or third-party components.
