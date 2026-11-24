import json

import httpx
import pytest

from soulsync_tg_bot.config import Config
from soulsync_tg_bot.soulsync import SoulSync, SoulSyncError, playlist_link, track_key

SPOTIFY_ID = "37i9dQZF1DXcBWIGoYBM5M"
SONG = {
    "id": "abc",
    "name": "Time",
    "artists": [{"name": "Pink Floyd"}],
    "album": {"name": "Dark Side", "images": []},
    "duration_ms": 420000,
}


def test_config_requires_numeric_allowlist():
    env = {
        "TELEGRAM_BOT_TOKEN": "1:fake",
        "SOULSYNC_API_KEY": "key",
        "SOULSYNC_URL": "http://soulsync:8008",
        "TELEGRAM_ALLOWED_USER_IDS": "12,42",
    }
    assert Config.from_env(env).allowed_ids == {12, 42}
    for ids in ("", "12,,42", "@friend", "0"):
        with pytest.raises(ValueError):
            Config.from_env(env | {"TELEGRAM_ALLOWED_USER_IDS": ids})


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com.evil.test/playlist/" + SPOTIFY_ID,
        "https://x@open.spotify.com/playlist/" + SPOTIFY_ID,
        "https://open.spotify.com:443/playlist/" + SPOTIFY_ID,
        "https://open.spotify.com/album/" + SPOTIFY_ID,
        "https://open.spotify.com/playlist/%33" + SPOTIFY_ID[1:],
        "https://deezer.com.example.org/playlist/123456",
        "https://deezer.com@host.example.org/playlist/123456",
        "https://www.deezer.com/en/album/123456",
    ],
)
def test_rejects_unsupported_playlist_urls(url):
    with pytest.raises(SoulSyncError):
        playlist_link(url)


def test_spotify_share_url_and_duplicate_key():
    assert playlist_link(f"https://open.spotify.com/playlist/{SPOTIFY_ID}?si=abc") == (
        "spotify",
        SPOTIFY_ID,
    )
    assert track_key("The Beyoncé & Jay-Z", "Déjà Vu (Live) - Edit") == ("beyonce", "deja vu")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [{}, {"incomplete": True}, {"public": False}, {"expected_total": 2}, {"track_count": True}],
)
async def test_spotify_fetch_and_copy_contract(patch):
    requests = []

    def server(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer key"
        if request.method == "GET":
            assert request.url.path == f"/api/spotify/playlist/{SPOTIFY_ID}"
            return httpx.Response(
                200,
                json={
                    "id": SPOTIFY_ID,
                    "name": "Mix",
                    "owner": "Spotify",
                    "public": True,
                    "incomplete": False,
                    "expected_total": 1,
                    "track_count": 1,
                    "tracks": [SONG],
                }
                | patch,
            )
        assert request.url.path == "/api/user-playlists/7/tracks"
        assert json.loads(request.content) == {
            "allow_duplicates": False,
            "tracks": [
                {
                    "track_name": "Time",
                    "artist_name": "Pink Floyd",
                    "album_name": "Dark Side",
                    "duration_ms": 420000,
                    "source": "spotify_public",
                    "source_id": "abc",
                }
            ],
        }
        return httpx.Response(
            200, json={"success": True, "added": 1, "duplicates": [], "track_count": 2}
        )

    async with httpx.AsyncClient(
        base_url="http://soulsync", transport=httpx.MockTransport(server)
    ) as http:
        api = SoulSync(http, "key")
        if patch:
            with pytest.raises(SoulSyncError):
                await api.spotify(SPOTIFY_ID)
            assert len(requests) == 1
        else:
            preview = await api.spotify(SPOTIFY_ID)
            assert (await api.add(7, preview.tracks)).added == 1


@pytest.mark.asyncio
async def test_deezer_rejects_incomplete_playlist():
    def server(request):
        assert request.method == "GET"
        assert request.url.path == "/api/deezer/playlist/123456"
        return httpx.Response(
            200,
            json={
                "id": "123456",
                "name": "Mix",
                "owner": "me",
                "track_count": 2,
                "tracks": [SONG],
            },
        )

    async with httpx.AsyncClient(
        base_url="http://soulsync", transport=httpx.MockTransport(server)
    ) as http:
        with pytest.raises(SoulSyncError, match="verify the full playlist"):
            await SoulSync(http, "key").deezer("123456")


@pytest.mark.asyncio
async def test_write_timeout_is_ambiguous_and_never_retried():
    attempts = []

    def server(request):
        attempts.append(request.method)
        raise httpx.ReadTimeout("internal detail")

    async with httpx.AsyncClient(
        base_url="http://soulsync", transport=httpx.MockTransport(server)
    ) as http:
        with pytest.raises(SoulSyncError) as err:
            await SoulSync(http, "key").create("Mix")
    assert err.value.uncertain
    assert attempts == ["POST"]
