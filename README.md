# Illyriad Notifier

A self-hosted Illyriad notification inbox with town/type rules and ntfy forwarding.
One container, one persistent volume, one administrator. Independent of Quartermaster.

## Included

- Inbox with text, town, type, overall category, date, unread and delivery filters.
- Notification details, read/unread controls and delivery attempt history.
- Ordered rules with type/category/town selectors, topic overrides, priority and
  previews against the latest 200 notifications.
- Settings UI for Illyriad and ntfy credentials, connection tests, polling, retention,
  forwarding pause/resume, friendly ID labels and administrator password changes.
- SQLite-backed deduplication, retry state and history; encrypted API credentials.
- Initial history import without forwarding. No matching rule means inbox only.

## Run the published image

Download `compose.release.yaml` and `.env.example` from the latest GitHub release.
Rename `.env.example` to `.env` and set a unique administrator password. Then run:

```sh
podman compose -f compose.release.yaml up -d
```

The image is `ghcr.io/jon-holderman/illyriad-notifier:0.1.1`, built for Linux
AMD64 and ARM64. Docker Compose supports the same file. Use a version tag or
immutable digest for deployments; `latest` tracks tagged releases.

## Build locally with Podman Compose

```sh
cp .env.example .env
# Edit .env and set NOTIFIER_ADMIN_PASSWORD to a unique 12–200 character password.
podman compose up --build -d
```

Open http://localhost:8080 and sign in with that password. Docker Compose works
with the same specification. The container runs as UID 10001; the named volume
persists its database, encryption/session keys, and delivery state.

1. In **Settings**, save your Recent Notifications key (`elgea-NOTIF-…`, not the
   entire URL). The worker begins fetching automatically; **Fetch / test saved
   feed** uses the same hourly minimum.
2. Inspect the imported inbox. Existing events are labeled **Initial history**
   and will not be forwarded.
3. Save your ntfy origin, topic and optional access token. Use **Send ntfy test**
   to publish one test message. Subscribe to that topic in your ntfy app.
4. In **Rules**, select types/categories/towns, preview, and save. Add friendly
   names in Settings when the feed supplies only IDs.
5. Enable forwarding in Settings. Matching newly imported events will be sent;
   any events already queued while paused will also be sent.

Illyriad's key must stay associated with the installation's original player.
Key rotation is supported; a different player requires another data volume.
The supplied account key is not included in this repository or auto-configured.

### Network access

The default port binding is localhost. For a trusted LAN, set
`NOTIFIER_BIND_ADDRESS` to the host's LAN address and restart Compose. For access
through a TLS reverse proxy, set `NOTIFIER_SECURE_COOKIE=true` and provide HTTPS.
The app doesn't manage certificates or proxy deployment. Configure your actual
ntfy origin in Settings; loopback inside the container refers to the container.

HTTP ntfy origins are supported for private networks; use HTTPS when credentials
or notifications cross an untrusted network. The app fetches Illyriad over HTTPS.

## Rule semantics

Rules run from lowest order to highest; equal order uses the rule ID. The first
enabled matching rule wins. Selected IDs within one field are alternatives;
different fields must all match. No selection in a field means any value.
An **Inbox only** rule can stop a broader forwarding rule later in the list.
Town ID `-1` appears as **No town** for events without a specific town.

Previews check the candidate rule alone and never send messages. Saving a rule
changes decisions for future imports only. Each event keeps its original reason,
topic and priority. Global ntfy URL/token changes apply to queued messages.

Status meanings:

| Status | Meaning |
| --- | --- |
| Initial history | Imported during the first successful fetch; forwarding suppressed |
| Inbox only | A keep rule matched, or no rule matched |
| Pending | Queued, paused, or waiting for a retry |
| Sent | ntfy accepted the request |
| Failed | Five attempts failed; open the event to retry |

A notification's read/unread state is independent of forwarding.

## Reliability and limits

- Polling has a conservative 60-minute minimum, including manual tests, failures,
  and restarts. It is a local policy; current upstream limits and incremental
  fetching semantics still need verification.
- Illyriad may only expose a limited history window. Events removed upstream
  during a long outage cannot be recovered by this app.
- Five attempts use delays of 2, 4, 8 and 16 minutes before the next attempt.
  The worker checks due work every 15 seconds. Failed deliveries remain available
  for manual retries. Pausing delivery does not discard pending events.
- Delivery is **at-least-once**. A timeout after ntfy accepted a message, or a crash
  between acceptance and the SQLite commit, can cause a duplicate. Normal repeated
  feed entries and restarts do not replay already recorded events.
- Retention deletes successful/inbox message bodies and their attempt history
  after the configured days from import. Pending/failed events are preserved.
  Compact seen-ID records remain indefinitely to prevent historical replays.
- All displayed times are UTC. Timezone-free Illyriad timestamps are interpreted
  as UTC. Feed HTML becomes plain text; scripts, remote images and markup never run.
- Run only one application process against each data volume; do not use multiple
  uvicorn workers or replicas. A filesystem lock rejects a second worker process.

## Operations

```sh
podman compose logs notifier
podman compose down
# Interactive password recovery (also revokes existing sessions):
podman compose run --rm notifier illyriad-notifier reset-password
```

`GET /healthz` checks the app/database. Feed health appears in Settings, and each
notification shows its delivery history. A healthy HTTP process alone does not
prove either upstream service is working. Access logs are disabled and external
errors are redacted to prevent credentials in URLs from reaching logs.

`NOTIFIER_ADMIN_PASSWORD` is used only for a fresh database. Changing it later
won't change the password. After bootstrap, it can be removed from `.env` and the
container recreated. Use Settings or the recovery command to change a password.
Five failed sign-ins trigger a five-minute global lockout.

Back up the **entire volume**, including `secrets.json` and `notifier.sqlite3`.
Stop the container first for a simple consistent filesystem backup, then restart
it. Losing the encryption key makes saved credentials unrecoverable. Do not run
`compose down -v` unless you intend to discard all history and configuration.

## Local development

Python 3.13 and uv are required.

```sh
uv sync --frozen
uv run illyriad-notifier init
uv run illyriad-notifier serve
```

Use fictional data in a separate directory for UI development:

```sh
uv run illyriad-notifier --data-dir /tmp/notifier-demo init
uv run illyriad-notifier --data-dir /tmp/notifier-demo seed-demo
uv run illyriad-notifier --data-dir /tmp/notifier-demo serve --port 8081
```

Demo statuses and labels are simulated. Use a fresh data directory for a real
account. Tests have fake feeds and HTTP transports and never contact Illyriad or
publish real notifications.

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

See [the architecture decision](docs/decisions/0001-standalone-notifier.md) for
scope, boundaries, delivery guarantees and deferred features.

## Releases

GitHub Actions runs tests, lint, formatting, typing, and a container startup
check before publishing an image. Main builds receive a full commit-SHA tag;
`v*` Git tags matching the project version publish versioned multi-architecture
images and a GitHub release with the Compose bundle. All builds use GitHub-hosted
runners; they do not need homelab access or deployment credentials.
