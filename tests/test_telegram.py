import asyncio
import json
import time

import httpx
import pytest
from telegram import Update
from telegram.request import BaseRequest

from soulsync_tg_bot.app import build_application
from soulsync_tg_bot.config import Config
from soulsync_tg_bot.soulsync import SoulSync

SPOTIFY_ID = "37i9dQZF1DXcBWIGoYBM5M"


class RecordingTelegram(BaseRequest):
    def __init__(self):
        self.messages = {}
        self.last = {}
        self.next_id = 100
        self.commands = []
        self.fail_next_edit_with = ""

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 5

    async def do_request(self, url, method, request_data=None, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if api_method == "setMyCommands":
            self.commands = [item["command"] for item in params["commands"]]
        if api_method == "getMe":
            result = {"id": 999, "first_name": "Bot", "username": "testbot", "is_bot": True}
        elif api_method in {"sendMessage", "editMessageText"}:
            if (
                api_method == "editMessageText"
                and self.fail_next_edit_with
                and self.fail_next_edit_with in params["text"]
            ):
                self.fail_next_edit_with = ""
                return 503, b'{"ok":false,"error_code":503,"description":"Temporarily unavailable"}'
            identifier = params.get("message_id", self.next_id)
            self.next_id += 1
            chat = int(params["chat_id"])
            result = {
                "message_id": identifier,
                "date": 1,
                "chat": {"id": chat, "type": "private"},
                "text": params["text"],
                "from": {"id": 999, "first_name": "Bot", "is_bot": True},
            }
            self.messages[identifier] = result | {"markup": params.get("reply_markup")}
            self.last[chat] = identifier
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def button(self, chat, label):
        identifier = self.last[chat]
        for row in self.messages[identifier]["markup"]["inline_keyboard"]:
            for button in row:
                if button["text"] == label:
                    return identifier, button["callback_data"]
        raise AssertionError(f"No button {label}")


class ChatDriver:
    def __init__(self, app, telegram):
        self.app = app
        self.telegram = telegram
        self.sequence = 0

    async def update(self, *, user=12, text=None, reply=None, tap=None, private=True):
        self.sequence += 1
        actor = {"id": user, "first_name": "Tester", "is_bot": False}
        if tap:
            identifier, data = tap
            payload = {
                "callback_query": {
                    "id": str(self.sequence),
                    "from": actor,
                    "chat_instance": "chat",
                    "data": data,
                    "message": self.telegram.messages[identifier],
                }
            }
        else:
            msg = {
                "message_id": self.sequence,
                "date": 1,
                "from": actor,
                "chat": {"id": user if private else -12, "type": "private" if private else "group"},
                "text": text,
            }
            if text.startswith("/"):
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
            if reply:
                msg["reply_to_message"] = self.telegram.messages[reply]
            payload = {"message": msg}
        await self.app.process_update(
            Update.de_json({"update_id": self.sequence} | payload, self.app.bot)
        )

    async def tap(self, label, user=12):
        await self.update(user=user, tap=self.telegram.button(user, label))

    async def reply(self, text):
        await self.update(text=text, reply=self.telegram.last[12])

    async def wait_for(self, text, *, user=12):
        async with asyncio.timeout(3):
            while text not in self.telegram.messages[self.telegram.last[user]]["text"]:
                await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["new", "existing"])
@pytest.mark.parametrize("provider", ["spotify", "deezer"])
async def test_import_via_telegram_buttons_and_allowlist(tmp_path, destination, provider):
    telegram = RecordingTelegram()
    writes = []
    tracks = []

    def server(req):
        route = req.url.path
        if route == "/api/deezer/playlist/123456":
            assert provider == "deezer"
            assert req.url.params["async"] == "1"
            return httpx.Response(
                202,
                json={
                    "pending": True,
                    "job_id": "deezer_playlist_123abc456def",
                    "playlist_id": "123456",
                    "status": "queued",
                },
            )
        if route == "/api/deezer/playlist-load/deezer_playlist_123abc456def":
            return httpx.Response(
                200,
                json={
                    "job_id": "deezer_playlist_123abc456def",
                    "playlist_id": "123456",
                    "status": "complete",
                    "playlist": {
                        "id": "123456",
                        "name": "Road Trip",
                        "owner": "me",
                        "track_count": 1,
                        "tracks": [
                            {
                                "id": "42",
                                "name": "Time",
                                "artists": ["Pink Floyd"],
                                "album": "The Dark Side of the Moon",
                                "duration_ms": 413000,
                                "album_cover_url": "https://cdn.example.com/cover.jpg",
                            }
                        ],
                    },
                },
            )
        if route == f"/api/spotify/playlist/{SPOTIFY_ID}":
            assert provider == "spotify"
            return httpx.Response(
                200,
                json={
                    "id": SPOTIFY_ID,
                    "name": "Road Trip",
                    "owner": "me",
                    "public": True,
                    "incomplete": False,
                    "expected_total": 1,
                    "track_count": 1,
                    "tracks": [{"id": "song", "name": "Time", "artists": [{"name": "Pink Floyd"}]}],
                },
            )
        if req.method == "POST":
            writes.append(route)
            tracks.extend(json.loads(req.content)["tracks"])
            if provider == "deezer":
                assert tracks == [
                    {
                        "track_name": "Time",
                        "artist_name": "Pink Floyd",
                        "album_name": "The Dark Side of the Moon",
                        "duration_ms": 413000,
                        "source": "deezer",
                        "source_id": "42",
                        "image_url": "https://cdn.example.com/cover.jpg",
                    }
                ]
            return httpx.Response(
                200, json={"success": True, "id": 7, "added": 1, "duplicates": [], "track_count": 1}
            )
        if route == "/api/user-playlists":
            return httpx.Response(
                200, json={"playlists": [{"id": 7, "name": "Shared", "track_count": len(tracks)}]}
            )
        assert route == "/api/mirrored-playlists/7"
        return httpx.Response(
            200, json={"id": 7, "source": "soulsync", "name": "Shared", "tracks": tracks}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://ss"
    ) as http:
        app, controller = build_application(
            Config("999:test", frozenset({12}), "http://ss", "key", tmp_path / "state.db"),
            api=SoulSync(http, "key"),
            request=telegram,
        )
        async with app:
            driver = ChatDriver(app, telegram)
            await driver.update(user=42, text="/start")
            assert "denied" in telegram.messages[telegram.last[42]]["text"].lower()
            await driver.update(text="/start")
            await driver.tap("Import playlist")
            link = (
                f"https://open.spotify.com/playlist/{SPOTIFY_ID}?si=secret"
                if provider == "spotify"
                else "https://www.deezer.com/en-us/playlist/123456?utm_source=share"
            )
            await driver.reply(link)
            if destination == "new":
                await driver.tap("Create new playlist")
            else:
                await driver.tap("Add to existing")
                await driver.tap("Shared · 0")
            confirm = telegram.button(12, "Import songs")
            assert len(confirm[1].encode()) <= 64
            if destination == "new":
                telegram.fail_next_edit_with = "1 added"
            await driver.update(user=42, tap=confirm)
            await driver.update(tap=confirm)
            await driver.update(tap=confirm)
            await driver.wait_for("1 added")
            await controller.stop()
        assert writes == [
            "/api/user-playlists" + ("/7/tracks" if destination == "existing" else "")
        ]


@pytest.mark.asyncio
async def test_create_search_add_sync_and_attach_second_user(tmp_path):
    telegram = RecordingTelegram()
    playlist = {"id": 7, "source": "soulsync", "name": "Mix & <Friends>", "tracks": []}
    state = {"playlist_id": 7, "status": "idle"}
    posts = []

    def server(req):
        route = req.url.path
        if req.method == "POST":
            posts.append(route)
            body = json.loads(req.content)
            if route == "/api/user-playlists":
                assert body == {"name": "Mix & <Friends>"}
                return httpx.Response(200, json={"success": True, "id": 7})
            if route.endswith("/tracks"):
                playlist["tracks"].extend(body["tracks"])
                assert body["allow_duplicates"] is False
                return httpx.Response(
                    200, json={"success": True, "added": 1, "duplicates": [], "track_count": 1}
                )
            assert route.endswith("/pipeline/run") and body == {}
            state.update(
                status="running",
                started_at=time.time(),
                run_id="profile:7",
                progress=35,
                phase="Phase 2/4: Identifying...",
            )
            return httpx.Response(200, json={"success": True, "state": state})
        if route == "/api/user-playlists":
            rows = [{"id": 7, "name": playlist["name"], "track_count": len(playlist["tracks"])}]
            return httpx.Response(200, json={"playlists": rows if posts else []})
        if route.endswith("/pipeline/status"):
            return httpx.Response(200, json=state)
        if route.endswith("/search_tracks"):
            assert req.url.params["query"] == "Pink Floyd"
            return httpx.Response(
                200,
                json={
                    "tracks": [
                        {
                            "id": "song",
                            "name": "Time",
                            "artists": ["Pink Floyd"],
                            "album": "Dark Side",
                        }
                    ]
                },
            )
        assert route == "/api/mirrored-playlists/7"
        return httpx.Response(200, json=playlist)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://ss"
    ) as http:
        app, controller = build_application(
            Config(
                "999:test",
                frozenset({12, 13}),
                "http://ss",
                "key",
                tmp_path / "state.db",
                poll_seconds=1,
            ),
            api=SoulSync(http, "key"),
            request=telegram,
        )
        async with app:
            driver = ChatDriver(app, telegram)
            await controller.startup(app)
            assert telegram.commands == ["start", "playlists", "import", "cancel", "help"]
            await driver.update(text="/start", private=False)
            assert "private chat" in telegram.messages[telegram.last[-12]]["text"]
            await driver.update(text="/start")
            await driver.tap("Create playlist")
            await driver.reply("Mix & <Friends>")
            await driver.wait_for("Created")
            await driver.tap("View playlist")
            await driver.tap("Add songs")
            await driver.reply("Pink Floyd")
            await driver.tap("1. Pink Floyd — Time")
            await driver.wait_for("Added to")
            await driver.tap("View playlist")
            await driver.tap("Sync & download")
            assert posts == ["/api/user-playlists", "/api/user-playlists/7/tracks"]
            await driver.tap("Start sync & download")
            await driver.wait_for("35%")
            await driver.update(user=13, text="/playlists")
            await driver.tap("Mix & <Friends> · 1", user=13)
            await driver.tap("View sync status", user=13)
            await driver.wait_for("35%", user=13)
            state.update(
                status="finished",
                progress=100,
                result={"tracks_synced": "1", "wishlist_queued": "0"},
            )
            await driver.wait_for("finished")
            await driver.wait_for("finished", user=13)
            assert "Mix &amp; &lt;Friends&gt;" in telegram.messages[telegram.last[12]]["text"]
            await controller.stop()
    assert posts == [
        "/api/user-playlists",
        "/api/user-playlists/7/tracks",
        "/api/mirrored-playlists/7/pipeline/run",
    ]
    assert playlist["tracks"][0]["source_id"] == "song"
