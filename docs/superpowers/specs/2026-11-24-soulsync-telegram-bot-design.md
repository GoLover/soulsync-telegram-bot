# SoulSync Telegram Bot Design

Date: 2026-11-24

## Intent

Build a standalone Python Telegram bot that gives a small, explicitly allowed
set of Telegram users a shared conversational surface over one existing
SoulSync installation. Users create SoulSync-owned playlists, search SoulSync's
music catalogue, add exact search results to those playlists, and deliberately
start SoulSync's existing "Sync & download" pipeline.

SoulSync remains the source of truth for playlists, tracks, discovery, media
server synchronization, and downloads. The bot's SQLite database stores only
Telegram-facing state, short-lived search snapshots, update deduplication, and
pipeline-monitoring metadata.

## Goals

- Restrict every bot action to an environment-configured allowlist of numeric
  Telegram user IDs.
- Let an allowed user list shared SoulSync playlists and create a new one.
- Let the user select a playlist, search for songs, page through results, and
  add an exact result with one tap.
- Let the user inspect the current playlist contents.
- Require a confirmation before starting SoulSync's "Sync & download" pipeline.
- Show honest pipeline phases and real progress by editing one Telegram message.
- Survive bot restarts without losing user navigation or abandoning a monitored
  pipeline.
- Be easy to deploy beside an existing SoulSync instance using Docker Compose
  or plain Python long polling.

## Non-goals

- Installing, configuring, or modifying SoulSync.
- Reimplementing SoulSync playlist, matching, sync, wishlist, or download logic.
- Reading or writing SoulSync's database directly.
- Supporting Telegram groups, inline mode, webhooks, Mini Apps, or HTML widgets.
- Playlist deletion, renaming, reordering, track removal, cover art, schedules,
  or automation management in the first release.
- Managing Telegram access from inside the bot; operators change the allowlist
  in configuration and restart the bot.
- Streaming audio or media files through Telegram.

## Anti-goals

- Never authorize by username or display name.
- Never expose or persist the Telegram token or SoulSync API key in SQLite,
  logs, callbacks, or Telegram messages.
- Never duplicate SoulSync playlist data as an independent local playlist
  system.
- Never silently retry a playlist creation, track addition, or pipeline start
  when the first request's outcome is unknown.
- Never show invented progress percentages.
- Never leave a Telegram callback spinner, working message, or destructive
  confirmation in an unresolved state.

## Constraints and compatibility

- Python 3.11 or newer.
- `python-telegram-bot` 22.8, using its async `Application` and long polling.
- `httpx` for async HTTP calls to SoulSync.
- Python's standard-library `sqlite3` module for storage; no ORM.
- Private chats only.
- Telegram callback data stays ASCII and within the 64-byte Bot API limit.
- SoulSync is called with `Authorization: Bearer <api-key>`.
- The design was checked against SoulSync commit
  `6fdd64e9e94f85e4b2409e66b0b5449c7c3a4e3a` (2026-11-21). The bot performs a
  startup authentication/read-shape check. SoulSync does not expose an OpenAPI
  or non-mutating capabilities contract for all required writes, so each write
  route is also shape-checked on first use and fails with a version-specific
  configuration error rather than guessing.
- Telegram UX guidance was checked against the `telegram-bot-ui` skill at
  commit `36309f518902097cf5a449c067fa93308052b8db`, Bot API 10.5.

## Approaches considered

### 1. Standalone long-polling sidecar — selected

Run a separate Python process beside SoulSync. It receives Telegram updates via
long polling and calls SoulSync's existing HTTP API. This needs no inbound HTTPS
route, isolates failures from SoulSync, and can be upgraded or restarted
independently. SQLite is entirely bot-owned.

Tradeoff: the bot depends on several SoulSync UI endpoints rather than only the
smaller public v1 API, so capability checks and a narrow API adapter are
required.

### 2. Standalone webhook service

Expose an HTTPS endpoint for Telegram updates. This is appropriate for a public,
horizontally scaled service but adds TLS, reverse-proxy, secret-path, and ingress
operations that a single self-hosted SoulSync installation does not need.

### 3. Embed Telegram handling inside SoulSync

Register bot lifecycle and handlers in SoulSync's Flask process. This reduces
the number of containers but couples bot uptime and upgrades to SoulSync and
makes upstream SoulSync updates harder to adopt.

The user selected approach 1.

## Runtime architecture

```text
Telegram
   │ long polling
   ▼
python-telegram-bot Application
   ├── allowlist/update middleware
   ├── command, text, and callback handlers
   ├── screen renderers + callback router
   ├── per-playlist async locks
   ├── SQLite repository
   └── SoulSyncClient (httpx)
              │ Bearer API key
              ▼
        Existing SoulSync HTTP API
              ├── playlists and tracks
              ├── catalogue search
              └── pipeline run/status
```

The Telegram layer knows only view models returned by `SoulSyncClient`. Route
paths, response normalization, timeouts, and SoulSync error parsing stay inside
that adapter. This contains the effect of future SoulSync API changes.

## Configuration

Required environment variables:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`: comma-separated signed integer IDs; startup fails
  if empty or malformed.
- `SOULSYNC_URL`: base URL such as `http://soulsync:8008`.
- `SOULSYNC_API_KEY`: an admin/write-capable SoulSync API key.

Optional environment variables:

- `SOULSYNC_SEARCH_SOURCE`: `spotify` (default), `deezer`, `itunes`, or
  `musicbrainz`.
- `DATABASE_PATH`: default `/data/bot.sqlite3` in Docker and
  `./data/bot.sqlite3` outside Docker.
- `SOULSYNC_HTTP_TIMEOUT_SECONDS`: default 20.
- `PIPELINE_POLL_SECONDS`: default 2.5, clamped to a safe minimum.
- `LOG_LEVEL`: default `INFO`.

Secrets are read only from the environment. Logs redact authorization headers,
URLs containing credentials, and raw inbound message bodies.

## SoulSync API contract

All requests carry the Bearer API key. Expected routes:

| Action | Method and route | Important request/response data |
|---|---|---|
| Capability/list check | `GET /api/user-playlists` | `{playlists: [...]}` |
| Create playlist | `POST /api/user-playlists` | `{name}` → `{success: true, id}` |
| Read playlist | `GET /api/mirrored-playlists/{id}` | playlist plus `tracks` |
| Search tracks | `GET /api/{source}/search_tracks` | `query`, `limit` → `tracks` |
| Add track | `POST /api/user-playlists/{id}/tracks` | `tracks`, `allow_duplicates: false` → `added`, `duplicates`, `track_count` |
| Start pipeline | `POST /api/mirrored-playlists/{id}/pipeline/run` | `{}` → `{success: true, state}` |
| Poll pipeline | `GET /api/mirrored-playlists/{id}/pipeline/status` | status, phase, progress, result/error |

The user-playlist list contains `id`, `name`, integer `track_count`, optional
`image_url`, `cover_tiles`, and `liked`. Playlist detail contains the mirror row
plus `tracks`; track renderers tolerate missing optional metadata. Search routes
return `{tracks: [...]}` where a normalized track requires `id`, `name`, at
least one artist, and may include `album`, `duration_ms`, `image_url`, `source`,
and `disambiguation`. Provider-specific artist strings/lists and album
strings/objects are normalized inside `SoulSyncClient`.

For an added result the bot sends title, primary artist string, album, duration,
image URL, source, and source ID. These fields let SoulSync preserve the exact
catalogue origin when available instead of identifying only by free text.

An add response with `added: 1` is success. `added: 0` plus a non-empty
`duplicates` array is "Already in playlist"; each duplicate may carry
`track_name`, `artist_name`, `existing_track_name`, and
`existing_artist_name`. Other shapes are treated as incompatible, not success.

Pipeline status is normalized to local `idle`, `running`, `finished`,
`skipped`, or `error`. SoulSync currently emits `idle`, `running`, `finished`,
`skipped`, and `error`; the adapter treats unknown values as protocol errors.
`progress` is clamped to an integer 0–100, `phase` is plain text, `started_at`
and `finished_at` are Unix timestamps, and `result`/`error` are nullable. Final
result keys currently include string counts such as `tracks_synced`,
`sync_skipped`, and `wishlist_queued`. The bot displays the normalized phase and
known result counters only; it does not forward arbitrary raw pipeline log
lines, paths, provider errors, or trace text to Telegram.

The configured search source is explicit. Authentication/configuration failures
are shown as actionable errors rather than silently switching to a different
catalogue with different results.

`SoulSyncClient` maps connection errors, timeouts, 401/403, 404, 409, validation
errors, and unexpected 5xx responses into typed exceptions with safe user
messages. It never retries non-idempotent writes automatically. Read-only list,
detail, search, and status calls may retry once for transient connect/timeout
failures with short jitter.

The relevant SoulSync writes do not accept an idempotency key. The bot therefore
promises idempotent user intent where it can verify the resulting state, not
network-level exactly-once delivery. Unknown write outcomes enter an explicit
`unknown` state and are reconciled with reads; they are never blindly retried.

SoulSync collapses playlist-name whitespace, requires a non-empty name, truncates
to 200 characters, and adds ` (2)`, ` (3)`, and so on when a user-playlist name
already exists. The bot mirrors the empty/200-character validation but always
renders the name returned by SoulSync after creation.

## Allowlist and chat boundaries

An outer update guard runs before every command, message, and callback handler:

1. Require `effective_user.id` to be in the parsed numeric allowlist.
2. Require a private chat for application screens.
3. For an unauthorized `/start`, return a generic "Access denied" response;
   ignore later unauthorized updates without revealing SoulSync state.
4. For an allowed user in a group, answer with a short request to open the bot
   privately and perform no SoulSync action.
5. Re-check the allowlist and ownership of server-side bot state on every
   callback; old buttons are untrusted input.

The SQLite `users` row is observational state, not authority. Removing an ID
from the environment allowlist takes effect after restart even if the user still
exists in SQLite.

## SQLite design

SQLite uses WAL mode, foreign keys, a busy timeout, explicit transactions, and
`PRAGMA user_version` migrations. Each repository call opens a short-lived
connection. Small synchronous calls are acceptable; handler code keeps
transactions short and never holds one across HTTP requests.

### `users`

- `telegram_user_id INTEGER PRIMARY KEY`
- `first_seen_at TEXT NOT NULL`
- `last_seen_at TEXT NOT NULL`

### `sessions`

- `telegram_user_id INTEGER PRIMARY KEY REFERENCES users`
- `selected_playlist_id INTEGER NULL`
- `screen TEXT NOT NULL`
- `screen_version INTEGER NOT NULL DEFAULT 1`
- `pending_input TEXT NULL` (`playlist_name` or `search_query`)
- `back_state_json TEXT NOT NULL DEFAULT '{}'`
- `updated_at TEXT NOT NULL`

### `searches`

- `search_id TEXT PRIMARY KEY`: short random URL-safe token
- `telegram_user_id INTEGER NOT NULL REFERENCES users`
- `playlist_id INTEGER NOT NULL`
- `query TEXT NOT NULL`
- `source TEXT NOT NULL`
- `results_json TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `expires_at TEXT NOT NULL`

Search snapshots expire after 30 minutes and are cleaned opportunistically.
They keep result callbacks short and ensure page/detail/back actions refer to the
same result set.

### `processed_updates`

- `update_id INTEGER PRIMARY KEY`
- `telegram_user_id INTEGER NULL`
- `created_at TEXT NOT NULL`

This handles redelivery of the same Telegram update across a process failure.
Rows are pruned after seven days.

### `action_nonces`

- `nonce TEXT PRIMARY KEY`: short random token embedded in a prompt or callback
- `telegram_user_id INTEGER NOT NULL REFERENCES users`
- `kind TEXT NOT NULL`
- `target_key TEXT NOT NULL`
- `status TEXT NOT NULL`: `pending`, `inflight`, `succeeded`, `unknown`, `failed`
- `created_at TEXT NOT NULL`
- `consumed_at TEXT NULL`

The nonce is consumed transactionally before a semantic write. It prevents two
distinct callback updates from a real double tap from both starting a pipeline,
and binds a playlist-name reply to the prompt that requested it. Add-song
actions use a stable target key of playlist/search/result; SoulSync's
`allow_duplicates: false` remains the final guard.

### `pipeline_operations`

- `operation_id TEXT PRIMARY KEY`
- `playlist_id INTEGER NOT NULL`
- `requested_by INTEGER NOT NULL REFERENCES users`
- `run_id TEXT NULL`
- `status TEXT NOT NULL`: includes `starting`, `running`, terminal states, and `unknown`
- `last_phase TEXT NULL`
- `last_progress INTEGER NULL`
- `request_started_at TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `finished_at TEXT NULL`
- `telegram_finalized_at TEXT NULL`

Only one non-terminal local operation may exist per playlist. SoulSync itself
also enforces its global pipeline lock.

### `pipeline_views`

- `operation_id TEXT NOT NULL REFERENCES pipeline_operations`
- `telegram_user_id INTEGER NOT NULL REFERENCES users`
- `chat_id INTEGER NOT NULL`
- `message_id INTEGER NOT NULL`
- `last_render_hash TEXT NULL`
- `PRIMARY KEY (operation_id, telegram_user_id, chat_id)`

The initiating user gets the first view. A second allowed user opening the same
playlist while a bot-known run is active can attach a read-only status view;
the one monitor fans state changes out to all views. Starting is never offered
again. On startup, the bot resumes rows that are non-terminal **or** have a null
`telegram_finalized_at`, so a crash between persisting completion and sending
the final Telegram edit cannot lose the result.

## Concurrency and idempotency

- A per-playlist `asyncio.Lock` serializes bot-originated add and pipeline-start
  actions in one process.
- SoulSync remains authoritative and has its own playlist edit lock; after a
  successful add, the bot refetches the playlist rather than incrementing a
  local count optimistically.
- Every callback is answered immediately before HTTP work begins.
- `processed_updates` blocks literal redelivery; single-use `action_nonces`
  block separate updates representing the same user action.
- A create action stores the pre-request playlist-ID set. After an unknown HTTP
  outcome it refreshes the list: exactly one new ID is adopted, no new ID is a
  visible unknown/failure, and multiple concurrent new IDs require the user to
  verify from the list. The bot never resends automatically.
- An unknown add outcome is reconciled by refetching the playlist and checking
  the normalized selected track key. The goal is "the track is present", so a
  concurrent successful add is an acceptable reconciliation.
- Before pipeline start, the bot snapshots that playlist's status and records
  the request timestamp. After a timeout it attaches only if the same
  playlist's state transitioned from idle/terminal to running with a
  `started_at` at or after this attempt; otherwise the operation becomes
  `unknown` and directs the operator to SoulSync rather than retrying.
- A definite `409` is never retried. If a matching bot-known operation exists,
  the user may attach a view. If SoulSync's global lock is owned by an external
  or unknown playlist, the API exposes no reliable owner identity, so the bot
  says another pipeline is running and links the recovery guidance to the
  SoulSync UI; it does not offer a misleading Status target.
- Pipeline finalization is idempotent: terminal state is persisted before the
  final Telegram edit, and a restart may safely try that final edit again.

## Commands

- `/start` — open the home screen.
- `/playlists` — open the playlist list.
- `/cancel` — leave the current text-entry flow and return to the relevant
  screen; it does not cancel a SoulSync pipeline.
- `/help` — concise usage and configuration-owner contact guidance.

Commands and buttons are usable only in private chats by allowed users.

## Screen and state map

All normal navigation edits one bot message in place to keep the chat clean.
Free-text prompts create a new ForceReply message because a reply field cannot
be added reliably by editing an old screen. Every non-root screen has one Back
action in the last row. At depth greater than two, a Menu action is also shown.

| Screen ID | Body and keyboard | State | Exits and variants |
|---|---|---|---|
| `home` | One-line purpose; `My playlists` (primary), `Create playlist` | none | playlist list, create prompt, unavailable error |
| `playlist.list` | Six shared playlists per page with counts | page, snapshot version | playlist detail, pager, create, empty state, home |
| `playlist.create.ask` | "Name your playlist" with ForceReply | pending input | create result, validation error on same step, cancel |
| `playlist.create.result` | Created name and current count | playlist ID | add songs (primary), view playlist, list |
| `playlist.detail` | Name, track count, up to ten numbered track summaries | playlist ID, page/version | add songs, Sync & download, refresh, track pager, list; missing playlist variant |
| `search.ask` | Selected playlist and "What song are you looking for?" with ForceReply | playlist ID, pending input | results, validation/no-result/error, cancel |
| `search.results` | Query, playlist, five numbered results with artist/album/duration | search ID, page/version | add exact result, pager, new search, playlist detail |
| `search.added` | "Added" or "Already in playlist" plus fresh count | playlist ID, search ID | add another, back to results, view playlist |
| `pipeline.confirm` | Playlist name/count and consequence: identify, push to server, download missing | playlist ID/version | explicit `Start sync & download` (success style), back |
| `pipeline.running` | One edited status message: real percentage when supplied and normalized phase | operation ID | terminal result; additional users can attach read-only views; stale confirm cannot start again |
| `pipeline.result` | Completed/skipped/failed summary | operation ID | view playlist, retry only after failure and a fresh confirmation, menu |
| `help` | Short workflow and command list | none | home |
| `error` | Safe cause and recovery action | previous screen | retry read, back, menu |

Search result buttons use the shape `1. Artist — Title`; album and duration stay
in the message body. Five results per page keep labels readable on narrow
screens. A no-op page indicator answers with a toast because the selected SDK
does not expose Bot API 10.3 disabled buttons. Button styles are used only for
one primary forward action, the final sync confirmation, and never as the only
signal.

Representative callback namespaces:

- `pl:list:<page>:<version>`
- `pl:view:<id>:<page>:<version>`
- `sr:page:<search-id>:<page>:<version>`
- `sr:add:<search-id>:<index>:<version>`
- `pipe:ask:<playlist-id>:<version>`
- `pipe:go:<playlist-id>:<nonce>`
- `nav:home` and `nav:back:<token>`

All callback fields are parsed and range-checked. User-entered text and names
never appear in callback data.

## Telegram feedback behavior

- Call `answerCallbackQuery` before any SoulSync HTTP request.
- For short searches/list fetches, use Telegram's typing chat action.
- For pipeline start, replace the confirmation immediately with a starting
  state and remove the armed confirmation keyboard.
- Poll at the configured interval but edit only when phase/progress/status
  changes, avoiding more than one edit per second and honoring Telegram
  `retry_after` responses.
- Use SoulSync's real `progress` when present; otherwise show named phases only.
- Finish every path with a real terminal message: success, skipped/busy, error,
  or monitoring interrupted with a retry/status action.
- All outbound HTML is escaped through one renderer. User and SoulSync strings
  are data, never markup or instructions.

## Failure behavior

- **SoulSync unavailable:** keep the user's screen state; show Retry and Back.
- **Invalid/expired API key:** report a configuration error without echoing the
  key and suppress repeated noisy stack traces.
- **Incompatible SoulSync:** startup names read/auth shape failures and exits
  non-zero; a write-route mismatch is reported on first use as a version
  incompatibility and is not retried.
- **No playlists:** offer Create playlist as the only primary action.
- **Invalid playlist name:** preserve the reply step and explain the length or
  empty-name rule.
- **No search results:** keep the selected playlist and offer New search or
  Back.
- **Expired/stale search button:** toast that results expired and open a fresh
  search prompt; never add by stale array position.
- **Duplicate track:** show "Already in playlist" using SoulSync's duplicate
  response; allow a new search.
- **Playlist changed/deleted:** refetch, invalidate local selection, and return
  to the shared list.
- **Pipeline globally busy:** do not queue or retry. Attach only to a matching
  bot-known operation; otherwise explain that the active pipeline can be
  inspected in SoulSync and offer Back.
- **Pipeline error:** show SoulSync's safe error summary and require a new
  confirmation before retrying.
- **Telegram edit failed because the message is gone:** persist the operation
  and send one replacement terminal/status message, then update its message ID.

## Project layout

```text
src/soulsync_tg_bot/
  __init__.py
  __main__.py
  app.py
  config.py
  access.py
  callbacks.py
  db.py
  models.py
  soulsync.py
  handlers/
    commands.py
    playlists.py
    search.py
    pipeline.py
    errors.py
  ui/
    keyboards.py
    render.py
    screens.py
tests/
  unit/
  integration/
docs/
Dockerfile
docker-compose.example.yml
pyproject.toml
.env.example
README.md
```

## Deployment

The image runs one bot process with a persistent `/data` volume. The example
Compose file shows the common case where the bot and SoulSync share a Docker
network, but it does not start or reconfigure SoulSync. A plain-Python path uses
the same environment variables and data directory.

Startup order:

1. Parse configuration and refuse an empty allowlist.
2. Initialize/migrate SQLite.
3. Probe SoulSync authentication and the read-only user-playlist/mirror response
   shapes. Mutating endpoints are validated on first use because SoulSync has no
   complete non-mutating capabilities endpoint.
4. Register the four Telegram commands.
5. Resume non-terminal pipeline monitors.
6. Start long polling with only the required update types.

Graceful shutdown stops accepting updates, cancels poll tasks after persisting
their last state, closes the shared `httpx.AsyncClient`, and closes the
Telegram application.

## Testing and verification

### Unit tests

- Configuration parsing, allowlist edge cases, and secret-safe errors.
- SQLite migrations, transactions, expiry cleanup, and pipeline resumption.
- Access guard for unauthorized, allowed-private, and allowed-group updates.
- Callback parsing, 64-byte maximum, version/nonces, ownership, and stale state.
- HTML escaping, clipping-before-escaping, pagination, empty/error screens.
- Search result normalization for all four configured sources.
- Telegram update redelivery, semantic double taps with different callback IDs,
  create/add/pipeline unknown-outcome reconciliation, and per-playlist locking.
- Pipeline state transitions and terminal finalization.
- Multiple pipeline views and crash recovery while final notification is still
  pending.

### Integration tests

- `httpx.MockTransport` exercises every SoulSync route, status family, timeout,
  malformed response, duplicate track, 409 pipeline, and pipeline progression.
- Telegram handlers run against fabricated PTB updates and a recording bot
  request layer; no real token is required.
- A fake SoulSync HTTP app covers the complete create → search → add → confirm →
  pipeline flow and restart/resume behavior.

### Static and packaging checks

- Ruff lint/format check.
- Mypy on the application package.
- Pytest with branch coverage over critical access, write, and pipeline paths.
- Build the Docker image and run its startup check against the fake SoulSync
  server.
- Validate representative keyboards and callbacks with the installed
  `telegram-bot-ui` validators.

Evidence without user credentials will be offline contract and integration
testing, not a claim that Android/iOS/Desktop/Web rendering or a live SoulSync
installation was exercised. A final optional smoke-test checklist will explain
how the operator can test with a dedicated Telegram bot and their installed
SoulSync.

## Builder decisions left open

- Exact class/function names and whether closely related handler files should
  be combined.
- The migration helper's internal structure.
- Exact neutral microcopy, provided the screen map and action meaning remain.
- Test fixture organization and the concrete fake Telegram transport.
- Whether to use `uv`, pip, or another standards-compatible installer in local
  developer instructions; the distributable remains a normal `pyproject.toml`
  package.

