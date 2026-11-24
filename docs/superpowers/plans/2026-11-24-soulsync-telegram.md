# SoulSync Telegram Implementation Plan

> Execute inline with focused tests first and an independent final review.

**Goal** Build the shared playlist bot described in the approved design.

**Architecture** A single polling process handles Telegram. A typed HTTP adapter calls SoulSync, and SQLite holds sessions, action tokens, and pending operation records.

**Dependencies** Python 3.11+, python-telegram-bot 22.8, HTTPX, sqlite3.

**Spec** [SoulSync Telegram Bot Design](../specs/2026-11-24-soulsync-telegram-bot-design.md)

## Delivery

- [x] Write contract tests for URL validation, Spotify completeness, exact copy payloads, and write timeouts. Build the configuration and HTTP adapter in `config.py` and `soulsync.py`.
- [x] Exercise one-use actions, owner checks, snapshot expiry, and recovery across reopened SQLite connections. Implement storage in `db.py` and write/recovery coordination in `operations.py`.
- [x] Exercise allowlist enforcement and actual callback routing with fabricated Telegram updates. Build the command, reply and button flows in `app.py`, `screens.py` and `telegram_ui.py`.
- [x] Package the bot with a Python entry point and a single-process Compose example. Run the full test suite, type checker, linter and packaging checks, then run independent review.

Verified with Python 3.12, simulated Telegram and SoulSync APIs, and a locally
built wheel. Docker image build and live service verification require the
deployment environment.

## Review focus

- Check Spotify links and metadata before enabling writes, including incomplete and private playlists.
- Recover a committed copy after a timeout without issuing another POST.
- Keep definite write success after a failure fetching the resulting playlist.
- Reject stale or foreign callbacks before they can mutate shared playlists.
- Distinguish one pipeline run from another using its start timestamp.
