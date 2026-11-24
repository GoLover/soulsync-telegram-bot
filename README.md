# SoulSync Telegram bot

A Python Telegram bot for one existing SoulSync installation. Everyone on the
Telegram allowlist can view and edit the same SoulSync playlists in private
chats. The bot keeps chat state in SQLite. SoulSync handles the music catalogue,
playlists, media-server sync, and downloads.

## Setup

1. Create a Telegram bot with [BotFather](https://t.me/BotFather) and get its token.
2. Get a write-capable API key from your SoulSync installation. The bot uses
   the key's SoulSync profile for every allowed Telegram user.
3. Copy `.env.example` to `.env`. Set `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric Telegram user IDs),
   `SOULSYNC_URL`, and `SOULSYNC_API_KEY`. Keep the file private.
4. Run one instance using Docker or Python.

### Docker Compose

Set `SOULSYNC_DOCKER_NETWORK` to your existing SoulSync container network and
use the SoulSync service address and port in `SOULSYNC_URL`. Then run:

```sh
docker compose -f docker-compose.example.yml up -d --build
```

The Compose example runs only the bot. The named `bot-data` volume holds the
SQLite database. The bot receives updates via long polling and needs no
published ports. Run one replica for a Telegram token and SQLite database.

### Python 3.11+

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install .
set -a
. ./.env
set +a
python -m soulsync_tg_bot
```

Source only a configuration file you control. Quote values as necessary for
your shell. Set `DATABASE_PATH` if you want to store SQLite somewhere other
than `./data/bot.sqlite3`.

## Using the bot

- Open a private chat and send `/start`. Choose **Create playlist** or open
  **My playlists**.
- In a playlist, tap **Add songs**, reply with a song or artist, and tap one
  of SoulSync’s search results to add it.
- Use **Import playlist** or `/import`. Paste a public Spotify or Deezer
  playlist URL, preview the songs, then create an editable playlist or add them
  to an existing one. This is a one-time copy. SoulSync skips duplicate songs
  and the bot reports what it added. For a shortened Deezer share link, open it
  in a browser and paste the resulting `deezer.com/playlist/…` URL.
- Open a playlist and confirm **Sync & download**. SoulSync updates the media
  server and queues missing songs using its existing configuration.
- Use `/cancel` to leave a reply prompt and `/help` to see the workflow again.

Spotify import uses the Spotify integration configured in SoulSync. Deezer
import uses SoulSync’s public playlist loader. The bot rejects incomplete imports
and applies the same 1,000-song default limit to both providers. Set
`SPOTIFY_IMPORT_MAX_TRACKS` to a value from 1 to 5,000 to change it. If loading a
large Deezer playlist times out, increase `SOULSYNC_IMPORT_TIMEOUT_SECONDS`.
Search
uses Spotify by default and can use `deezer`, `itunes`, or `musicbrainz`
through `SOULSYNC_SEARCH_SOURCE`.

The bot uses SoulSync UI endpoints. The implementation targets SoulSync commit
`6fdd64e9e94f85e4b2409e66b0b5449c7c3a4e3a`. Startup checks API access and
playlist responses. If a write's outcome is uncertain, the bot checks the
playlist through SoulSync and preserves the result across its own restart.
If it still cannot confirm the result, use the read-only check on the import
message or inspect the playlists in SoulSync.

## Development

```sh
python -m pip install -e '.[dev]'
pytest --cov=soulsync_tg_bot --cov-branch
ruff check .
ruff format --check .
mypy src
```

To smoke-test a deployment, start with a dedicated bot token and test playlist.
Add a search result, import a small public Spotify playlist into a new and an
existing playlist, and confirm a sync on the test playlist. Check the copied
tracks and the server result in SoulSync.
