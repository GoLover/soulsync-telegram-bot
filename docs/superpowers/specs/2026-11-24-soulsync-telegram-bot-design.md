# SoulSync Telegram Bot Design

Date: 2026-11-24

## Intent

Build a standalone Python Telegram bot that gives a small, explicitly allowed
set of Telegram users a shared conversational surface over one existing
SoulSync installation. Users create SoulSync-owned playlists, search SoulSync's
music catalogue, add exact search results to those playlists, copy a public
Spotify playlist into a new or existing editable playlist, and deliberately
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
- Let the user paste a public Spotify playlist URL, preview the snapshot, and
  copy its songs into a new playlist or append them to an existing playlist.
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
- Private Spotify playlists, Spotify account browsing, Spotify albums, and
  keeping an imported playlist linked to later Spotify changes.

## Anti-goals

- Never authorize by username or display name.
- Never expose or persist the Telegram token or SoulSync API key in SQLite,
  logs, callbacks, or Telegram messages.
- Never duplicate SoulSync playlist data as an independent local playlist
  system.
- Never silently retry a playlist creation, track addition, or pipeline start
  when the first request's outcome is unknown.
- Never describe a Spotify snapshot import as synchronized, mirrored, or live.
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

### Spotify import semantics — one-time editable copy selected

Three import models were considered:

1. Validate the public link, fetch its complete tracks through SoulSync's
   configured Spotify integration, then copy the normalized tracks into a
   SoulSync user playlist. This works for both a new and an existing
   destination, needs no Spotify credentials in the bot, and leaves the result
   editable.
2. Create a `spotify_public` mirrored playlist. This would remain refreshable,
   but cannot naturally mean "append into this existing user playlist" and
   conflicts with the requested one-time behavior.
3. Call Spotify directly from the bot. This duplicates SoulSync's link parsing,
   introduces another authentication/API compatibility surface, and is
   unnecessary because the installed SoulSync already exposes the data.

The user selected model 1. "Import" therefore always means a frozen snapshot;
the normal SoulSync pipeline may later identify, sync, and download that copy.

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
- `SOULSYNC_IMPORT_TIMEOUT_SECONDS`: default 60 for Spotify fetching and the
  single playlist-copy request; it must be at least the general HTTP timeout.
- `SPOTIFY_IMPORT_MAX_TRACKS`: default 1000, accepted range 1–5000. Oversized
  snapshots are rejected whole rather than silently truncated.
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
| Fetch Spotify playlist | `GET /api/spotify/playlist/{spotify_id}` | metadata, completeness markers, and `tracks` |
| Create playlist with imported tracks | `POST /api/user-playlists` | `name`, `tracks` → `id`, `added`, `duplicates`, `track_count` |
| Append imported tracks | `POST /api/user-playlists/{id}/tracks` | `tracks`, `allow_duplicates: false` → counts |
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

Before calling SoulSync, the bot parses at most 2048 characters with
`urllib.parse.urlsplit`: the scheme must be exactly `https`, the case-folded
hostname exactly `open.spotify.com`, userinfo and explicit ports are forbidden,
and the path must be exactly `/playlist/<id>` with an optional trailing slash.
The ID must match ASCII `[A-Za-z0-9]{10,64}`. Query parameters such as `si` and
all fragments are ignored; only the validated ID is sent to SoulSync as a path
segment. Spotify URIs, short links,
look-alike hosts, percent-encoded path tricks, albums, and additional path
segments are rejected. The bot extracts the ID and calls
`GET /api/spotify/playlist/{spotify_id}`. This route uses SoulSync's configured
Spotify client and its public full-fetch fallback, and unlike the public-link
parser it exposes whether fallback data is incomplete. A valid response
currently contains `id`, `name`, `owner`, boolean `public`, `track_count`,
`tracks`, boolean `incomplete`, and nullable integer `expected_total`. Each usable track is
normalized to the
same user-playlist shape as a search result, with `source: spotify_public` and
the Spotify track ID as `source_id`. Artist-object arrays and album objects are
normalized inside `SoulSyncClient`; malformed tracks without both title and
artist are counted as skipped.

Completeness is a hard gate: response `id` must equal the URL ID,
`public` must be exactly `true`, `incomplete` must be exactly `false`, `track_count` must be a non-negative
integer equal to `len(tracks)`, and when `expected_total` is an integer it must
also equal `track_count`. `name` must remain non-empty after whitespace
collapse and `tracks` must be a list. A 401 means the SoulSync installation
needs Spotify authentication for this route; `incomplete: true` asks the user
to retry later or inspect SoulSync. The bot never falls back to
`POST /api/spotify/parse-public`, because that endpoint can silently use an
embed response capped near 100 tracks and has no completeness flag. Any failed
invariant is never written to a playlist.

The complete source count and normalized snapshot must not exceed
`SPOTIFY_IMPORT_MAX_TRACKS`, and the serialized normalized track array must not
exceed 2 MiB. Either limit produces an oversized-playlist error before any
playlist write. The bot never truncates an import because a partial snapshot
would look deceptively complete.

Every imported `tracks` element sent to either write route has this exact base
JSON shape:

```json
{
  "track_name": "Track title",
  "artist_name": "Primary Artist, Featured Artist",
  "album_name": "Album title or empty string",
  "duration_ms": 183000,
  "source": "spotify_public",
  "source_id": "spotifyTrackId"
}
```

`track_name` and `artist_name` are non-empty, whitespace-collapsed strings;
artists are joined in response order with `, `. `album_name` is the collapsed
album name or `""`. `duration_ms` is a non-negative JSON integer in
milliseconds, using `0` when missing/invalid. `source` is the literal
`spotify_public`; `source_id` is the non-empty Spotify track ID, limited to 100
ASCII alphanumeric, dash, underscore, colon, or period characters. A source
track lacking a valid ID, title, or artist is excluded and counted in
`skipped_track_count`. The only optional key is `image_url`; it is included only
when it is an `http`/`https` string from SoulSync, otherwise it is omitted. No
fetch/search response object is forwarded directly to a write route.

An import is a one-time snapshot. For a new destination the bot submits exactly
`{"name": <spotify name>, "tracks": [...]}` in one
`POST /api/user-playlists` request. That route does not accept an
`allow_duplicates` flag; the tested SoulSync implementation calls
`add_tracks(..., allow_duplicates=False)` by default and returns `success`,
`id`, `added`, `duplicates`, and `track_count`. For an existing destination the
bot sends all tracks in one
`POST /api/user-playlists/{id}/tracks` request. Both writes use SoulSync's
default duplicate protection; matches under SoulSync's normalized artist/title
key are skipped, not inserted twice. After either definite success the bot always refetches
`GET /api/mirrored-playlists/{id}`; the detail response supplies the actual
collision-suffixed `display_name` and authoritative count for rendering. The
write response supplies exact added/already-present counts. Later Spotify
changes have no effect, while the copied SoulSync playlist remains editable
and can use the normal pipeline.

The new-destination route is one HTTP request but SoulSync internally creates
the empty user playlist before adding its tracks, so an error response can
still leave an empty or partial candidate. After an import request is
dispatched, any timeout, disconnect, malformed success body, or 5xx is treated
as an unknown outcome and reconciled. A new-destination non-2xx is also
reconciled for this reason. The bot never deletes an empty/partial candidate
automatically. Pre-handler 401/403 failures and an existing-destination 404 are
definite failures because the target handler did not perform the write.

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
detail, search, status, and Spotify-playlist fetch calls may retry once for
transient connect/timeout failures with short jitter.

The relevant SoulSync writes do not accept an idempotency key. The bot therefore
promises idempotent user intent where it can verify the resulting state, not
network-level exactly-once delivery. Unknown write outcomes enter an explicit
`unknown` state and are reconciled with reads; they are never blindly retried.

SoulSync collapses playlist-name whitespace, requires a non-empty name, truncates
to 200 characters, and adds ` (2)`, ` (3)`, and so on when a user-playlist name
already exists. The bot mirrors the empty/200-character validation but always
renders the name returned by SoulSync after creation.

For duplicate checks and import reconciliation, the adapter mirrors the tested
SoulSync `track_key` algorithm exactly and locks it with compatibility tests:
collapse input whitespace; remove bracketed title suffixes, a whitespace-dash
suffix, then `feat`, `ft`, `featuring`, or `with` suffixes; for artists use the
first component before comma, ampersand, `x`, `and`, semicolon, or slash; apply
Unicode NFKD, remove combining marks, lowercase, replace non-word characters
with spaces, and collapse spaces; finally remove a leading `the ` from the
artist. An all-bracket title falls back to its unstripped form. This key is
version-specific compatibility code, not a claim of musical identity. If
SoulSync's behavior at the pinned contract changes, import recovery fails safe
as incompatible instead of guessing.

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
- `pending_input TEXT NULL` (`playlist_name`, `search_query`, or `spotify_url`)
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

### `spotify_imports`

- `import_id TEXT PRIMARY KEY`: short random URL-safe token
- `telegram_user_id INTEGER NOT NULL REFERENCES users`
- `source_playlist_id TEXT NOT NULL`
- `name TEXT NOT NULL`
- `subtitle TEXT NULL`
- `tracks_json TEXT NOT NULL`: normalized, importable snapshot tracks only
- `source_track_count INTEGER NOT NULL`
- `skipped_track_count INTEGER NOT NULL`
- `created_at TEXT NOT NULL`
- `expires_at TEXT NOT NULL`

Import snapshots expire after two hours and are owned by the Telegram user who
parsed the URL. The raw inbound URL is not retained; the source playlist ID and
stored metadata are enough for display, callback validation, and retrying
navigation. Expiry removes only the bot's temporary snapshot, never the copied
SoulSync tracks.

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
`allow_duplicates: false` remains the final guard. Spotify import confirmations
use a stable destination/import target and cannot be reused with a different
playlist.

### `import_operations`

- `operation_id TEXT PRIMARY KEY`
- `import_id TEXT NOT NULL REFERENCES spotify_imports ON DELETE CASCADE`
- `requested_by INTEGER NOT NULL REFERENCES users`
- `destination_kind TEXT NOT NULL`: `new` or `existing`
- `destination_playlist_id INTEGER NULL`
- `requested_name TEXT NULL`
- `before_playlist_ids_json TEXT NOT NULL DEFAULT '[]'`
- `before_track_keys_json TEXT NOT NULL DEFAULT '[]'`
- `status TEXT NOT NULL`: `starting`, `written`, `succeeded`, `unknown`, or `failed`
- `result_playlist_id INTEGER NULL`
- `result_name TEXT NULL`
- `result_track_count INTEGER NULL`
- `added_count INTEGER NULL`
- `duplicate_count INTEGER NULL`
- `chat_id INTEGER NOT NULL`
- `message_id INTEGER NOT NULL`
- `last_render_hash TEXT NULL`
- `request_started_at TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `finished_at TEXT NULL`
- `telegram_finalized_at TEXT NULL`
- `expires_at TEXT NOT NULL`

The pre-write snapshots make an ambiguous timeout or process interruption
reconcilable without resending the write. Rendering `spotify.confirm`
pre-generates an operation ID, but stores it only in the pending nonce's bound
target. When `sp:go` is tapped, one SQLite transaction validates and consumes
the nonce, inserts the `starting` operation with the current Telegram message
target and pre-write snapshots, and extends the referenced import snapshot's
expiry. An abandoned confirmation therefore leaves no operation that recovery
could mistake for a dispatched write.

On a valid write response, one transaction immediately persists `written`,
`finished_at`, the returned playlist ID/counts, and the consumed nonce's success **before** the
mandatory detail refetch. A failed `GET /api/mirrored-playlists/{id}` therefore
cannot turn a known successful write into `unknown`, cannot trigger write
reconciliation, and can never enable another import. Detail success stores the
authoritative display name/count and advances `written` to `succeeded`; detail
failure renders a known-success variant with exact write counts and "details
temporarily unavailable," plus a read-only retry.

On startup, import recovery selects every `starting` row, every `written` row
still within retention, and every result row whose `telegram_finalized_at` is
null. It reconciles only `starting` rows and performs only the detail GET for
`written` rows. It persists a result state before editing the stored Telegram
message; if that message is gone, it sends one replacement and updates
`message_id`. Only after a result edit succeeds is `telegram_finalized_at` set.
This is the same persist-before-notify rule used for pipelines.

Unreferenced previews expire after two hours. Creating an operation extends its
preview and operation expiry to seven days; a `starting` row is never deleted
before startup recovery. A starting row older than 24 hours receives one final
read-only reconciliation and becomes `unknown` if still unresolved. Terminal
rows, including `written` and permanently unknown rows, are deleted after seven days, then
orphaned expired previews are removed. The foreign key cascade is a final
safety net, not the normal cleanup order.

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
- A process-wide creation lock serializes all bot-originated new-playlist
  writes, including ordinary create and new-destination Spotify imports. A
  create action stores the pre-request playlist-ID set. After an unknown HTTP
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
- A Spotify import persists its operation and destination snapshot in the
  nonce-consumption transaction before the
  copy request. For a new destination, reconciliation compares the pre/post
  playlist-ID sets and verifies the intended normalized track keys in the sole
  candidate. For an existing destination, it refetches that exact playlist and
  compares intended keys with the saved pre-write keys. If all intended keys
  (deduplicated by the same artist/title rule SoulSync uses) are present, the
  user's goal is complete. Exact added/already-present counts are shown only
  from a definite write response; reconciled success says all requested unique
  songs are present because an external concurrent writer could have added the
  same song. A partial or ambiguous result remains `unknown` and is never
  silently resent. Imports to one existing playlist share its per-playlist
  lock. New imports use the process-wide creation lock; playlist creation
  outside this bot can still make candidate selection ambiguous, which fails
  safe to `unknown`.

- After a transport timeout or restart, reconciliation performs read-only
  list/detail probes with bounded backoff at roughly 0, 1, 2, 4, 8, and 15
  seconds (30 seconds total). It never repeats the write. Startup resumes the
  remaining part of that window from `request_started_at`; after the window it
  persists and reports `unknown`. A user may request another read-only status
  reconciliation while the seven-day operation record remains, but a new
  import requires a new preview and confirmation.

- `sp:check` revalidates allowlist, operation ownership, message version, and
  retention before making reads. For `unknown`, it repeats the same
  destination reconciliation once; for `written`, it retries only the detail
  GET. If the state advances, one transaction stores the new state and clears
  `telegram_finalized_at`, the same message is rendered with the appropriate
  success variant, and finalization is recorded again after the edit. If it
  remains unresolved, the bot answers with the last checked time and keeps the
  no-resend keyboard. Expired operations return to the shared playlist list.

## Commands

- `/start` — open the home screen.
- `/playlists` — open the playlist list.
- `/import` — ask for a public Spotify playlist URL.
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
| `home` | One-line purpose; `My playlists` (primary), `Create playlist`, `Import from Spotify` | none | playlist list, create/import prompt, unavailable error |
| `playlist.list` | Six shared playlists per page with counts | page, snapshot version | playlist detail, pager, create, empty state, home |
| `playlist.create.ask` | "Name your playlist" with ForceReply | pending input | create result, validation error on same step, cancel |
| `playlist.create.result` | Created name and current count | playlist ID | add songs (primary), view playlist, list |
| `playlist.detail` | Name, track count, up to ten numbered track summaries | playlist ID, page/version | add songs, import Spotify here, Sync & download, refresh, track pager, list; missing playlist variant |
| `search.ask` | Selected playlist and "What song are you looking for?" with ForceReply | playlist ID, pending input | results, validation/no-result/error, cancel |
| `search.results` | Query, playlist, five numbered results with artist/album/duration | search ID, page/version | add exact result, pager, new search, playlist detail |
| `search.added` | "Added" or "Already in playlist" plus fresh count | playlist ID, search ID | add another, back to results, view playlist |
| `spotify.ask` | "Paste a public Spotify playlist link" with ForceReply and privacy/snapshot note | optional preselected playlist ID, pending input | preview, validation/private/unavailable error, cancel |
| `spotify.preview` | Spotify name, owner/subtitle, importable/skipped counts, and up to five sample songs | import ID/version | create new playlist, add to existing, cancel; expired snapshot variant |
| `spotify.destination` | Six existing shared user playlists per page; a preselected playlist is first | import ID, page/version | destination confirmation, pager, preview |
| `spotify.confirm` | Snapshot name/count and exact destination; says duplicates will be skipped and Spotify changes will not follow | import ID, destination, nonce | explicit `Import songs` (success style), back |
| `spotify.running` | Keyboard removed; one edited "Copying Spotify snapshot…" message | operation ID | import result or unknown-outcome guidance |
| `spotify.result` | Definite success: destination plus exact added/already-present/malformed-skipped counts. Reconciled success: known destination and "all N requested unique songs are present," with no invented split. Known-write/detail-pending: returned ID and exact write counts, but temporary name/detail warning. | operation ID/version | view playlist when ID is known, `Check details again` for `written`, import another, menu |
| `spotify.unknown` | Existing destination name when known, or an explicit "could not determine which new playlist" variant; never claims an exact destination or counts it cannot prove | operation ID/version | read-only `Check again` while retained, shared playlist list, menu; no resend action |
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
- `sp:show:<import-id>:<version>`
- `sp:pick:<import-id>:<page>:<version>`
- `sp:dest:<import-id>:<playlist-id>:<version>`
- `sp:go:<operation-id>:<nonce>`
- `sp:check:<operation-id>:<version>`
- `pipe:ask:<playlist-id>:<version>`
- `pipe:go:<playlist-id>:<nonce>`
- `nav:home` and `nav:back:<token>`

All callback fields are parsed and range-checked. User-entered text and names
never appear in callback data.

## Telegram feedback behavior

- Call `answerCallbackQuery` before any SoulSync HTTP request.
- For short searches/list fetches, use Telegram's typing chat action.
- Parsing a Spotify link uses the typing action and replaces the ForceReply
  prompt with a preview only after SoulSync returns a complete snapshot.
- Import confirmation is armed exactly once. On tap, answer the callback,
  remove the keyboard, and edit the message to `Copying Spotify snapshot…`;
  do not stream per-track edits for the single SoulSync import request.
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
- **Invalid Spotify URL/type:** keep the URL reply step and require a public
  `open.spotify.com/playlist/...` link; do not accept an album as a playlist.
- **Private/unavailable Spotify playlist:** explain that SoulSync could not read
  the public page and offer Retry, Back, and Menu without asking for Spotify
  credentials.
- **Spotify snapshot has no usable tracks:** do not create an empty playlist;
  show how many malformed tracks were skipped and return to URL entry.
- **Expired Spotify preview:** discard the old buttons and ask the user to paste
  the link again; no playlist write occurs.
- **Spotify import duplicates:** report SoulSync's added and duplicate counts;
  duplicate protection stays enabled for both new and existing destinations.
- **Unknown Spotify import outcome:** reconcile from the persisted before-state
  and playlist detail. If still ambiguous or partial, show the existing
  destination only when known; a new-destination ambiguity explicitly says the
  bot cannot determine which new playlist was created. While the seven-day
  record remains, `Check again` performs only list/detail reads. It never offers
  a one-tap resend of the full import.
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
    spotify_import.py
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
4. Register the five Telegram commands.
5. Reconcile unfinished/unnotified Spotify import operations and resume
   non-terminal pipeline monitors.
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
- Public Spotify URL validation, playlist-fetch response normalization,
  incomplete/expected-total rejection, unauthenticated/private/malformed
  rejection, import preview expiry, and skipped-track accounting.
- Exact duplicate-key compatibility fixtures covering whitespace, Unicode
  accents, punctuation, brackets, dash/feature suffixes, artist separators,
  and all-bracket titles.
- Telegram update redelivery, semantic double taps with different callback IDs,
  create/add/Spotify-import/pipeline unknown-outcome reconciliation, and
  per-playlist/process-wide creation locking.
- Spotify new-destination name collision handling, existing-destination
  pagination, duplicate reporting, one-time-copy wording, and stale destination
  buttons.
- Confirmation-not-tapped cleanup; every crash boundary around nonce
  consumption, operation insertion, dispatch, response persistence, and final
  Telegram edit; missing-message replacement and restart result delivery.
- Definite write success followed by detail timeout, `written` restart recovery,
  read-only detail retry, and proof that none of those paths can resend a write.
- Two allowed users creating destinations concurrently, an external SoulSync
  creation during reconciliation, delayed visibility after timeout, bounded
  reconciliation expiry, exact-vs-reconciled count wording, and seven-day
  operation/snapshot cleanup.
- URL host-confusion, userinfo, port, percent-encoding, path, query stripping,
  overlength input, response-ID mismatch, track-count limit, and serialized
  snapshot-size limit cases.
- Pipeline state transitions and terminal finalization.
- Multiple pipeline views and crash recovery while final notification is still
  pending.

### Integration tests

- `httpx.MockTransport` exercises every SoulSync route, status family, timeout,
  malformed response, complete and incomplete Spotify fetch shapes,
  new/existing import responses, duplicate track, 409 pipeline, and pipeline
  progression.
- Telegram handlers run against fabricated PTB updates and a recording bot
  request layer; no real token is required.
- A fake SoulSync HTTP app covers both complete workflows: create → search →
  add → confirm → pipeline, and Spotify URL → preview → new/existing
  destination → import → pipeline. It also covers restart/reconciliation
  behavior during an import write and pipeline monitoring.

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

