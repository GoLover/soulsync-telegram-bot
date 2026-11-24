import json
import time

import httpx
import pytest

from soulsync_tg_bot.db import Database
from soulsync_tg_bot.operations import Operations
from soulsync_tg_bot.soulsync import SoulSync

TRACK = {
    "track_name": "Time",
    "artist_name": "Pink Floyd",
    "album_name": "",
    "duration_ms": 0,
    "source": "spotify_public",
    "source_id": "a",
}


def test_only_current_owner_can_use_action(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    version = db.screen(12, 12)
    action = db.action(12, version, "confirm", {"id": 7})
    db.bind(12, version, 18)
    assert db.resolve(13, 13, 18, action) is None
    assert db.resolve(12, 12, 19, action) is None
    reopened = Database(db.path)
    assert reopened.resolve(12, 12, 18, action) == ("confirm", {"id": 7})
    reopened.screen(12, 12)
    assert db.resolve(12, 12, 18, action) is None


def test_expired_spotify_preview_is_unavailable_and_pruned(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    value = db.snapshot(12, "spotify", {"tracks": [TRACK]}, -1)
    assert db.load_snapshot(12, "spotify", value) is None
    db.prune()
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["import_new", "import_existing"])
async def test_write_return_persisted_before_detail_timeout(tmp_path, kind):
    posts = []

    def server(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(
                200, json={"success": True, "id": 7, "added": 1, "duplicates": [], "track_count": 1}
            )
        if request.url.path == "/api/user-playlists":
            return httpx.Response(200, json={"playlists": []})
        if kind == "import_existing" and not posts:
            return httpx.Response(
                200, json={"id": 7, "name": "Mix", "source": "soulsync", "tracks": []}
            )
        raise httpx.ReadTimeout("detail failed")

    db = Database(tmp_path / "bot.sqlite")
    async with httpx.AsyncClient(
        base_url="http://ss", transport=httpx.MockTransport(server)
    ) as http:
        ops = Operations(db, SoulSync(http, "key"))
        params = {"name": "Mix", "tracks": [TRACK]}
        if kind == "import_existing":
            params["playlist_id"] = 7
        assert db.arm("operation", kind, 12, 12, 18, params)
        assert not db.arm("operation", kind, 12, 12, 18, params)
        await ops.execute("operation")
    row = Database(db.path).operation("operation")
    assert row and row["status"] == "written"
    assert row["data"]["result"]["playlist_id"] == 7
    if kind == "import_new":
        assert posts == [{"name": "Mix", "tracks": [TRACK]}]
    else:
        assert posts == [{"tracks": [TRACK], "allow_duplicates": False}]


@pytest.mark.asyncio
async def test_reconcile_committed_append_after_restart(tmp_path):
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx.Response(
            200, json={"id": 7, "name": "Mix", "source": "soulsync", "tracks": [TRACK]}
        )

    db = Database(tmp_path / "bot.sqlite")
    db.arm("operation", "import_existing", 12, 12, 18, {"tracks": [TRACK], "playlist_id": 7})
    db.save("operation", "starting", {"dispatch": time.time(), "before": []})
    async with httpx.AsyncClient(
        base_url="http://ss", transport=httpx.MockTransport(server)
    ) as http:
        await Operations(Database(db.path), SoulSync(http, "key")).recover("operation")
    row = db.operation("operation")
    assert row and row["status"] == "succeeded"
    assert row["data"]["result"]["present"] == 1
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_recovery_does_not_adopt_new_playlist_with_unrelated_tracks(tmp_path):
    def server(request):
        assert request.method == "GET"
        if request.url.path == "/api/user-playlists":
            return httpx.Response(
                200, json={"playlists": [{"id": 7, "name": "Mix", "track_count": 2}]}
            )
        unrelated = TRACK | {"track_name": "Wish You Were Here"}
        return httpx.Response(
            200, json={"id": 7, "name": "Mix", "source": "soulsync", "tracks": [TRACK, unrelated]}
        )

    db = Database(tmp_path / "bot.sqlite")
    db.arm("operation", "import_new", 12, 12, 18, {"name": "Mix", "tracks": [TRACK]})
    db.save("operation", "starting", {"dispatch": time.time(), "before": []})
    async with httpx.AsyncClient(
        base_url="http://ss", transport=httpx.MockTransport(server)
    ) as http:
        assert not await Operations(db, SoulSync(http, "key")).recover("operation")
    row = db.operation("operation")
    assert row and row["status"] == "unknown"
