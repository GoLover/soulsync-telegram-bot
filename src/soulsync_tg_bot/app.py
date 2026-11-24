import asyncio
import hashlib
import logging
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass

import httpx
from telegram import Bot, BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import BaseRequest

from .config import Config
from .db import WEEK, Database
from .operations import Operations
from .screens import Button, Screen, clip, home, operation_result, pager, safe, track_lines
from .soulsync import Row, SoulSync, SoulSyncError, clean, spotify_playlist_id
from .telegram_ui import show

LOG = logging.getLogger(__name__)
COMMANDS = {
    "start": "Open the menu",
    "playlists": "Browse shared playlists",
    "import": "Copy a public Spotify playlist",
    "cancel": "Leave the current prompt",
    "help": "How to use this bot",
}


@dataclass(frozen=True)
class Chat:
    user: int
    id: int
    bot: Bot
    message: int | None = None


class Controller:
    def __init__(self, config: Config, db: Database, api: SoulSync):
        self.config = config
        self.db = db
        self.api = api
        self.ops = Operations(db, api)
        self.users: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.last_prune = 0.0

    async def startup(self, app: Application) -> None:
        playlists = await self.api.playlists()
        if playlists:
            await self.api.detail(playlists[0]["id"])
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, description in COMMANDS.items()]
        )
        self.db.prune()
        for value in self.db.pending():
            self.launch(app.bot, value)

    async def stop(self) -> None:
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def screen(self, chat: Chat, screen: Screen) -> None:
        version = self.db.screen(chat.user, chat.id)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        clip(b.label, 68),
                        callback_data=self.db.action(chat.user, version, b.action, b.data),
                        style=b.style,
                    )
                    for b in row
                ]
                for row in screen.rows
                if row
            ]
        )
        message = await show(chat.bot, chat.id, chat.message, screen.text, keyboard)
        self.db.bind(chat.user, version, message)

    async def prompt(self, chat: Chat, kind: str, params: Row) -> None:
        prompts = {
            "create": ("Name your playlist.", "Playlist name"),
            "search": ("What song are you looking for?", "Artist or song title"),
            "spotify": (
                "Paste a public Spotify playlist link. This copies the songs once.",
                "https://open.spotify.com/playlist/…",
            ),
        }
        text, placeholder = prompts[kind]
        if params.get("name"):
            text = f"{clip(params['name'])}\n{text}"
        version = self.db.screen(chat.user, chat.id, prompt=kind, context=params)
        sent = await chat.bot.send_message(
            chat.id,
            text + "\nUse /cancel to go back.",
            reply_markup=ForceReply(input_field_placeholder=placeholder),
        )
        self.db.bind(chat.user, version, sent.message_id)

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat, query = update.effective_user, update.effective_chat, update.callback_query
        msg = update.effective_message
        if not user or not chat or not msg:
            if query:
                await query.answer()
            return
        command = (msg.text or "").split(maxsplit=1)[0:1] if update.message else []
        name = command[0].split("@", 1)[0].removeprefix("/") if command else ""
        if user.id not in self.config.allowed_ids:
            if query:
                await query.answer()
            if chat.type == "private" and name == "start":
                await context.bot.send_message(chat.id, "Access denied.")
            return
        if chat.type != "private":
            if query:
                await query.answer("Open the bot in a private chat.")
            elif name in COMMANDS:
                await context.bot.send_message(chat.id, "Open the bot in a private chat.")
            return
        # Resolve and acknowledge before waiting on a network request or a user's lock.
        if query:
            data = query.data if isinstance(query.data, str) else ""
            action = self.db.resolve(user.id, chat.id, msg.message_id, data)
            op_match = re.fullmatch(
                r"o:([A-Za-z0-9_-]{16}):(check|view|list|home|import|search|results)", data
            )
            if not action and not op_match:
                await query.answer("This menu expired. Use /start.")
                return
            await query.answer("Current page" if action and action[0] == "noop" else None)
        async with self.users[user.id]:
            if self.db.seen(update.update_id):
                return
            if time.monotonic() - self.last_prune > 3600:
                self.db.prune()
                self.last_prune = time.monotonic()
            target = Chat(user.id, chat.id, context.bot, msg.message_id if query else None)
            try:
                if query:
                    if op_match:
                        await self.operation_action(target, op_match[1], op_match[2])
                    elif resolved := self.db.resolve(user.id, chat.id, msg.message_id, data):
                        await self.route(target, *resolved, action_id=data)
                elif name in COMMANDS and msg.text and msg.text.startswith("/"):
                    await self.command(target, name)
                elif update.message and update.message.text:
                    reply = update.message.reply_to_message
                    await self.input(
                        target, update.message.text, reply.message_id if reply else None
                    )
            except SoulSyncError as exc:
                session = self.db.session(target.user, target.id)
                if not query and session and session.get("prompt"):
                    await target.bot.send_message(target.id, str(exc))
                    await self.prompt(target, session["prompt"], session["context"])
                else:
                    retry = []
                    if (
                        query
                        and not op_match
                        and action
                        and action[0]
                        in {
                            "list",
                            "view",
                            "destinations",
                            "destination",
                            "sync",
                            "preview",
                        }
                        and exc.status != 404
                    ):
                        retry = [[Button("Try again", action[0], action[1])]]
                    await self.screen(
                        target,
                        Screen(
                            safe(str(exc), 320),
                            retry
                            + [
                                [Button("My playlists", "list"), Button("Menu", "home")],
                            ],
                        ),
                    )

    async def command(self, chat: Chat, name: str) -> None:
        if name == "cancel":
            session = self.db.session(chat.user, chat.id)
            params = session["context"] if session else {}
            if session and session.get("prompt") and params.get("playlist_id"):
                await self.route(chat, "view", {"playlist_id": params["playlist_id"]})
            else:
                await self.screen(chat, home())
        elif name == "help":
            await self.screen(
                chat,
                Screen(
                    "<b>How to use SoulSync</b>\n"
                    "Open My playlists or create a playlist. Choose Add songs, then tap a search "
                    "result to add it. Use /import to copy songs from a public Spotify playlist. "
                    "When ready, open the playlist and choose Sync &amp; download.\n\n"
                    "/cancel leaves a text prompt. Ask the bot operator for access or setup help.",
                    [[Button("Menu", "home")]],
                ),
            )
        else:
            await self.route(
                chat, {"start": "home", "playlists": "list", "import": "spotify"}[name], {}
            )

    async def input(self, chat: Chat, text: str, reply: int | None) -> None:
        session = self.db.session(chat.user, chat.id)
        if not session or not session.get("prompt"):
            return
        if reply is None or reply != session["message_id"]:
            await chat.bot.send_message(chat.id, "Reply to the latest bot prompt, or use /cancel.")
            return
        kind, context = session["prompt"], session["context"]
        text = clean(text) if kind != "spotify" else text
        if kind == "create":
            if not 0 < len(text) <= 200:
                raise SoulSyncError("Enter a playlist name between 1 and 200 characters.")
            await self.begin(
                Chat(chat.user, chat.id, chat.bot, reply),
                session["version"],
                "create",
                {"name": text},
            )
            return
        if kind == "search" and not 0 < len(text) <= 200:
            raise SoulSyncError("Enter a song or artist name up to 200 characters.")
        await chat.bot.send_chat_action(chat.id, "typing")
        if kind == "search":
            tracks = await self.api.search(text)
            sid = self.db.snapshot(
                chat.user, "search", context | {"tracks": tracks, "query": text}, 1800
            )
            await self.search_results(chat, sid, 0)
        elif kind == "spotify":
            source = spotify_playlist_id(text)
            preview = asdict(await self.api.spotify(source))
            sid = self.db.snapshot(
                chat.user,
                "spotify",
                preview
                | {
                    "preselected": context.get("playlist_id"),
                },
                7200,
            )
            await self.spotify_preview(chat, sid, preview)

    def snapshot(self, chat: Chat, kind: str, value: str) -> Row:
        result = self.db.load_snapshot(chat.user, kind, value)
        if not result:
            raise SoulSyncError("This preview expired. Start a fresh search or import.")
        return result

    async def route(self, chat: Chat, action: str, data: Row, *, action_id: str = "") -> None:
        pid = data.get("playlist_id")
        page = data.get("page", 0)
        if action == "home":
            await self.screen(chat, home())
        elif action in {"list", "destinations"}:
            await self.playlist_list(chat, page, data.get("snapshot"))
        elif action in {"create", "spotify", "search"}:
            await self.prompt(chat, action, data)
        elif action == "view":
            await self.playlist_detail(chat, data["playlist_id"], page)
        elif action == "results":
            await self.search_results(chat, data["snapshot"], page)
        elif action == "add":
            snap = self.snapshot(chat, "search", data["snapshot"])
            await self.begin(
                chat,
                action_id,
                "add",
                {
                    "playlist_id": snap["playlist_id"],
                    "name": snap["name"],
                    "tracks": [snap["tracks"][data["index"]]],
                    "search": data["snapshot"],
                    "search_page": page,
                },
            )
        elif action == "preview":
            sid = data["snapshot"]
            await self.spotify_preview(chat, sid, self.snapshot(chat, "spotify", sid))
        elif action == "destination":
            await self.import_confirm(chat, data["snapshot"], pid)
        elif action == "copy":
            snap = self.snapshot(chat, "spotify", data["snapshot"])
            await self.begin(
                chat,
                action_id,
                "import_new" if pid is None else "import_existing",
                {
                    "playlist_id": pid,
                    "name": data["name"],
                    "tracks": snap["tracks"],
                    "skipped": snap["skipped"],
                },
            )
        elif action == "sync":
            await self.sync_confirm(chat, data["playlist_id"])
        elif action == "run":
            known = self.db.active_pipeline(data["playlist_id"])
            if known:
                await self.attach(chat, known)
            else:
                await self.begin(chat, action_id, "pipeline", data)

    async def playlist_list(self, chat: Chat, page: int, sid: str | None) -> None:
        snap = self.snapshot(chat, "spotify", sid) if sid else {}
        rows = await self.api.playlists()
        rows.sort(
            key=lambda row: (
                row["id"] != snap.get("preselected"),
                row["name"].casefold(),
                row["id"],
            )
        )
        pages = max(1, (len(rows) + 5) // 6)
        page = min(page, pages - 1)
        data = {"snapshot": sid} if sid else {}
        buttons = [
            [
                Button(
                    f"{clip(row['name'], 51)} · {row['track_count']:,}",
                    "destination" if sid else "view",
                    data | {"playlist_id": row["id"]},
                )
            ]
            for row in rows[page * 6 : page * 6 + 6]
        ]
        buttons.append(pager(page, pages, "destinations" if sid else "list", data))
        if sid:
            buttons += [
                [Button("Create new playlist", "destination", data)],
                [Button("Back", "preview", data), Button("Menu", "home")],
            ]
        else:
            buttons += [
                [Button("Create playlist", "create", style="primary" if not rows else None)],
                [Button("Import from Spotify", "spotify")],
                [Button("Back", "home")],
            ]
        title = "Choose a destination" if sid else "My playlists"
        await self.screen(
            chat,
            Screen(
                f"<b>{title}</b>"
                + (
                    "\nNo shared playlists yet."
                    if not rows
                    else "\nShared across this SoulSync bot."
                ),
                buttons,
            ),
        )

    async def playlist_detail(self, chat: Chat, pid: int, page: int) -> None:
        detail = await self.api.detail(pid)
        name, tracks = detail.get("display_name") or detail["name"], detail["tracks"]
        pages = max(1, (len(tracks) + 9) // 10)
        page = min(page, pages - 1)
        params = {"playlist_id": pid, "name": name}
        active = self.db.active_pipeline(pid)
        text = f"<b>{safe(name)}</b>\n{len(tracks):,} songs"
        if tracks:
            text += "\n\n" + track_lines(tracks[page * 10 : page * 10 + 10], page * 10)
        await self.screen(
            chat,
            Screen(
                text,
                [
                    [Button("Add songs", "search", params, "primary")],
                    [Button("Import Spotify here", "spotify", params)],
                    [Button("View sync status" if active else "Sync & download", "sync", params)]
                    if tracks
                    else [],
                    pager(page, pages, "view", {"playlist_id": pid}),
                    [
                        Button("Refresh", "view", {"playlist_id": pid, "page": page}),
                        Button("Back", "list"),
                    ],
                ],
            ),
        )

    async def search_results(self, chat: Chat, sid: str, page: int) -> None:
        snap = self.snapshot(chat, "search", sid)
        tracks = snap["tracks"]
        pages = max(1, (len(tracks) + 4) // 5)
        page = min(page, pages - 1)
        selected = tracks[page * 5 : page * 5 + 5]
        rows = [
            [
                Button(
                    f"{index + 1}. {clip(t['artist_name'], 26)} — {clip(t['track_name'], 35)}",
                    "add",
                    {"snapshot": sid, "index": index, "page": page},
                )
            ]
            for index, t in enumerate(selected, page * 5)
        ]
        text = f"<b>{safe(snap['query'])}</b>\nAdd to {safe(snap['name'])}"
        text += "\n\n" + (
            track_lines(selected, page * 5, metadata=True) if tracks else "No songs found."
        )
        params = {"playlist_id": snap["playlist_id"], "name": snap["name"]}
        rows += [
            pager(page, pages, "results", {"snapshot": sid}),
            [Button("New search", "search", params)],
            [Button("Back", "view", params), Button("Menu", "home")],
        ]
        await self.screen(chat, Screen(text, rows))

    async def spotify_preview(self, chat: Chat, sid: str, snap: Row) -> None:
        tracks, skipped = snap["tracks"], snap["skipped"]
        text = f"<b>{safe(snap['name'])}</b>\n{len(tracks):,} importable songs"
        if snap.get("owner"):
            text += f" · {safe(snap['owner'], 60)}"
        if skipped:
            text += f"\n{skipped:,} entries missing song details"
        text += "\n\n" + track_lines(tracks[:5]) + "\n\nChoose where to copy these songs."
        params = {"snapshot": sid}
        buttons = []
        if snap.get("preselected"):
            buttons.append(
                [
                    Button(
                        "Add to selected playlist",
                        "destination",
                        params | {"playlist_id": snap["preselected"]},
                        "primary",
                    )
                ]
            )
        buttons += [
            [Button("Create new playlist", "destination", params)],
            [Button("Add to existing", "destinations", params)],
            [
                Button("Back", "spotify", {"playlist_id": snap.get("preselected")}),
                Button("Menu", "home"),
            ],
        ]
        await self.screen(chat, Screen(text, buttons))

    async def import_confirm(self, chat: Chat, sid: str, pid: int | None) -> None:
        snap = self.snapshot(chat, "spotify", sid)
        name = snap["name"][:200]
        if pid is not None:
            detail = await self.api.detail(pid)
            name = detail.get("display_name") or detail["name"]
        destination = f"<b>{safe(name)}</b>" + (" (new playlist)" if pid is None else "")
        await self.screen(
            chat,
            Screen(
                f"<b>Import from Spotify</b>\nCopy {len(snap['tracks']):,} songs from "
                f"{safe(snap['name'])} to {destination}.\n\n"
                "One-time copy. SoulSync skips duplicate songs.",
                [
                    [
                        Button(
                            "Import songs",
                            "copy",
                            {"snapshot": sid, "playlist_id": pid, "name": name},
                            "success",
                        )
                    ],
                    [Button("Back", "preview", {"snapshot": sid}), Button("Menu", "home")],
                ],
            ),
        )

    async def sync_confirm(self, chat: Chat, pid: int) -> None:
        if value := self.db.active_pipeline(pid):
            await self.attach(chat, value)
            return
        detail = await self.api.detail(pid)
        name = detail.get("display_name") or detail["name"]
        params = {"playlist_id": pid, "name": name}
        if not detail["tracks"]:
            await self.playlist_detail(chat, pid, 0)
            return
        await self.screen(
            chat,
            Screen(
                f"<b>Sync &amp; download</b>\n{safe(name)} · {len(detail['tracks']):,} songs\n\n"
                "SoulSync will identify songs, sync the playlist to your server, "
                "and queue missing songs for download.",
                [
                    [Button("Start sync & download", "run", params, "success")],
                    [Button("Back", "view", params), Button("Menu", "home")],
                ],
            ),
        )

    async def begin(self, chat: Chat, value: str, kind: str, data: Row) -> None:
        assert chat.message is not None
        if not self.db.arm(value, kind, chat.user, chat.id, chat.message, data):
            existing = self.db.active_pipeline(data["playlist_id"]) if kind == "pipeline" else None
            if existing:
                await self.attach(chat, existing)
            return
        self.db.screen(chat.user, chat.id)
        self.launch(chat.bot, value)

    async def attach(self, chat: Chat, value: str) -> None:
        assert chat.message is not None
        self.db.screen(chat.user, chat.id)
        self.db.attach(value, chat.user, chat.id, chat.message)
        await self.notify(chat.bot, value, only=chat.user)

    def launch(self, bot: Bot, value: str) -> None:
        if value not in self.jobs:
            self.jobs[value] = asyncio.create_task(self.worker(bot, value))

    async def worker(self, bot: Bot, value: str) -> None:
        try:
            row = self.db.operation(value)
            if not row:
                return
            if row["status"] == "queued":
                if row["user_id"] not in self.config.allowed_ids:
                    self.db.save(value, "failed", {"error": "Access was removed."})
                    return
                if row["created_at"] < time.time() - WEEK:
                    self.db.save(value, "failed", {"error": "This confirmation expired."})
                    await self.notify(bot, value)
                    return
                await self.notify(bot, value)
                await self.ops.execute(value)
            elif row["status"] == "starting":
                await self.ops.resume(row)
            elif row["status"] == "written":
                await self.ops.refresh(value)
            failures = 0
            while True:
                delivered = await self.notify(bot, value)
                row = self.db.operation(value)
                if not row or row["status"] != "running":
                    if not delivered:
                        for delay in (1, 3, 10, 30):
                            await asyncio.sleep(delay)
                            if await self.notify(bot, value):
                                break
                    return
                await asyncio.sleep(max(self.config.poll_seconds, min(30, 2 ** min(failures, 5))))
                try:
                    await self.ops.poll(value)
                    if failures and (current := self.db.operation(value)):
                        self.db.save(value, current["status"], {"monitor_paused": False})
                    failures = 0
                except SoulSyncError:
                    failures += 1
                    if failures == 3:
                        self.db.save(value, "running", {"monitor_paused": True})
        except Exception as exc:  # noqa: BLE001 - background task boundary; omit sensitive details
            LOG.error("Operation worker stopped (%s).", type(exc).__name__)
        finally:
            self.jobs.pop(value, None)

    async def notify(self, bot: Bot, value: str, *, only: int | None = None) -> bool:
        row = self.db.operation(value)
        if not row:
            return True
        text, buttons = operation_result(row)
        keyboard = (
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(label, callback_data=f"o:{value}:{action}")
                        for label, action in group
                    ]
                    for group in buttons
                ]
            )
            if buttons
            else None
        )
        digest = hashlib.sha256(repr((text, buttons)).encode()).hexdigest()
        delivered = True
        for view in self.db.views(value):
            user = view["user_id"]
            if (
                user not in self.config.allowed_ids
                or (only is not None and only != user)
                or view["rendered"] == digest
            ):
                continue
            try:
                message = await show(bot, view["chat_id"], view["message_id"], text, keyboard)
            except TelegramError as exc:
                LOG.warning("Operation update could not be delivered (%s).", type(exc).__name__)
                delivered = False
                continue
            self.db.mark_view(value, user, view["message_id"], message, digest)
        return delivered

    async def operation_action(self, chat: Chat, value: str, action: str) -> None:
        row = self.db.operation(value)
        if (
            not row
            or time.time() - row["updated_at"] > WEEK
            or not any(
                v["user_id"] == chat.user
                and v["chat_id"] == chat.id
                and v["message_id"] == chat.message
                for v in self.db.views(value)
            )
        ):
            await chat.bot.send_message(chat.id, "This result expired. Use /playlists.")
            return
        result = row["data"].get("result") or {}
        fresh = Chat(chat.user, chat.id, chat.bot)
        if (
            action == "check"
            and row["user_id"] == chat.user
            and row["status"] in {"written", "unknown"}
        ):
            if await self.ops.recover(value):
                await self.notify(chat.bot, value)
            else:
                await chat.bot.send_message(
                    chat.id, "Checked again. SoulSync still has no further details."
                )
        elif action == "view" and (pid := result.get("playlist_id") or row["playlist_id"]):
            await self.playlist_detail(fresh, pid, 0)
        elif action == "search" and (pid := result.get("playlist_id") or row["playlist_id"]):
            await self.prompt(
                fresh,
                "search",
                {
                    "playlist_id": pid,
                    "name": result.get("name") or row["data"].get("name"),
                },
            )
        elif action == "results" and (sid := row["data"].get("search")):
            await self.search_results(fresh, sid, row["data"].get("search_page", 0))
        elif action in {"list", "home", "import"}:
            await self.route(fresh, "spotify" if action == "import" else action, {})


def build_application(
    config: Config, *, api: SoulSync | None = None, request: BaseRequest | None = None
) -> tuple[Application, Controller]:
    owned_http = None
    if api is None:
        owned_http = httpx.AsyncClient(
            base_url=config.soulsync_url,
            timeout=config.timeout,
            follow_redirects=False,
            trust_env=False,
        )
        api = SoulSync(
            owned_http,
            config.soulsync_key,
            source=config.search_source,
            import_timeout=config.import_timeout,
            max_tracks=config.import_max_tracks,
        )
    controller = Controller(config, Database(config.database), api)

    async def shutdown(_: Application) -> None:
        await controller.stop()
        if owned_http is not None:
            await owned_http.aclose()

    async def handle_error(_: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOG.error("Telegram update failed (%s).", type(context.error).__name__)

    builder = (
        Application.builder()
        .token(config.telegram_token)
        .concurrent_updates(16)
        .post_init(controller.startup)
        .post_stop(shutdown)
        .post_shutdown(shutdown)
    )
    if request is not None:
        builder.request(request)
    app = builder.build()
    app.add_handler(CommandHandler(list(COMMANDS), controller.handle))
    app.add_handler(CallbackQueryHandler(controller.handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, controller.handle))
    app.add_error_handler(handle_error)
    return app, controller
