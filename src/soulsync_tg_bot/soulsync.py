import asyncio
import json
import math
import random
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, TypeGuard
from urllib.parse import urlsplit

import httpx

Row = dict[str, Any]


class SoulSyncError(Exception):
    def __init__(self, message: str, *, status: int | None = None, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


INCOMPATIBLE = "SoulSync returned an unexpected response. Check the supported version."


def clean(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def integer(value: Any, minimum: int = 0) -> TypeGuard[int]:
    return type(value) is int and value >= minimum


def finite(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def playlist_link(text: str) -> tuple[str, str]:
    if len(text) <= 2048 and not re.search(r"[\x00-\x1f\x7f]", text):
        try:
            url = urlsplit(text.strip())
        except ValueError:
            url = None
        if url and url.scheme == "https":
            host = url.netloc.lower()
            if host == "open.spotify.com":
                if match := re.fullmatch(r"/playlist/([A-Za-z0-9]{10,64})/?", url.path):
                    return "spotify", match[1]
            elif host in {"deezer.com", "www.deezer.com"}:
                if match := re.fullmatch(
                    r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?playlist/([1-9][0-9]{0,19})/?",
                    url.path,
                    re.IGNORECASE,
                ):
                    return "deezer", match[1]
            elif host in {"link.deezer.com", "deezer.page.link"}:
                raise SoulSyncError(
                    "Open the Deezer share link in a browser and paste the deezer.com/playlist/… address."
                )
    raise SoulSyncError("Paste a public Spotify or Deezer playlist link.")


def track_key(artist: str, title: str) -> tuple[str, str]:
    def fold(value: str) -> str:
        chars = unicodedata.normalize("NFKD", value)
        chars = "".join(c for c in chars if not unicodedata.combining(c))
        return " ".join(re.sub(r"[^\w\s]", " ", chars.lower()).split())

    feat = r"\s+(feat\.?|ft\.?|featuring|with)\s+.*$"
    artist, title = clean(artist), clean(title)
    reduced = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title)
    reduced = re.sub(feat, "", re.sub(r"\s+-\s+.*$", "", reduced), flags=re.IGNORECASE)
    main = re.split(
        r"\s*(,|&|\bx\b|\band\b|;|/)\s*",
        re.sub(feat, "", artist, flags=re.IGNORECASE),
        flags=re.IGNORECASE,
    )[0]
    return (fold(main) or fold(artist)).removeprefix("the "), fold(reduced) or fold(title)


def normalize_track(row: Any, source: str, *, imported: bool = False) -> Row | None:
    if not isinstance(row, dict):
        return None
    sid = row.get("id")
    sid = str(sid) if type(sid) in {str, int} else ""
    title = clean(row.get("name"))
    artists = row.get("artists")
    if isinstance(artists, list):
        names = [clean(a.get("name") if isinstance(a, dict) else a) for a in artists]
        artist = ", ".join(filter(None, names))
    else:
        artist = clean(artists or row.get("artist"))
    if not (title and artist and re.fullmatch(r"[A-Za-z0-9_:.\-]{1,100}", sid)):
        return None
    album = row.get("album")
    name = clean(album.get("name") if isinstance(album, dict) else album)
    duration = row.get("duration_ms")
    if not imported and row.get("source") in ("spotify", "deezer", "itunes", "musicbrainz"):
        source = row["source"]
    result = {
        "track_name": title,
        "artist_name": artist,
        "album_name": name,
        "duration_ms": duration if integer(duration) else 0,
        "source": source,
        "source_id": sid,
    }
    image = row.get("image_url") or row.get("album_cover_url")
    if not image and isinstance(album, dict):
        images = album.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            image = images[0].get("url")
    if isinstance(image, str) and image.startswith(("https://", "http://")):
        result["image_url"] = image
    return result


@dataclass(frozen=True)
class Preview:
    source_id: str
    name: str
    owner: str
    tracks: list[Row]
    skipped: int


@dataclass(frozen=True)
class Write:
    playlist_id: int
    added: int
    duplicates: int
    track_count: int


class SoulSync:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key: str,
        *,
        source: str = "spotify",
        import_timeout: float = 60,
        max_tracks: int = 1000,
    ):
        self.http = http
        self.headers = {"Authorization": f"Bearer {key}"}
        self.source = source
        self.import_timeout = import_timeout
        self.max_tracks = max_tracks

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Row | None = None,
        params: Row | None = None,
        long: bool = False,
        create_copy: bool = False,
    ) -> Row:
        writing = method == "POST"
        options: Row = {"timeout": self.import_timeout} if long else {}
        for attempt in range(2):
            try:
                response = await self.http.request(
                    method, path, headers=self.headers, json=body, params=params, **options
                )
                break
            except httpx.RequestError:
                if not writing and not attempt:
                    await asyncio.sleep(random.uniform(0.15, 0.3))
                    continue
                raise SoulSyncError(
                    "Could not reach SoulSync. Try again later.", uncertain=writing
                ) from None
        status = response.status_code
        if not response.is_success:
            messages = {
                400: "SoulSync could not accept this request.",
                401: "SoulSync could not authorize this request. Check the API key.",
                403: "SoulSync denied this action. Check its configuration.",
                404: "SoulSync could not find this item or API route.",
                409: "Another sync is running. Check its status in SoulSync.",
                429: "The music provider is busy. Try again later.",
            }
            raise SoulSyncError(
                messages.get(status, "SoulSync could not complete this request."),
                status=status,
                uncertain=writing and (status >= 500 or (create_copy and status not in {401, 403})),
            )
        try:
            data = response.json()
        except ValueError:
            raise SoulSyncError(INCOMPATIBLE, uncertain=writing) from None
        if not isinstance(data, dict):
            raise SoulSyncError(INCOMPATIBLE, uncertain=writing)
        return data

    async def playlists(self) -> list[Row]:
        data = await self._request("GET", "/api/user-playlists")
        rows = data.get("playlists")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not integer(row.get("id"), 1)
            or not clean(row.get("name"))
            or not integer(row.get("track_count"))
            for row in rows
        ):
            raise SoulSyncError(INCOMPATIBLE)
        return rows

    async def detail(self, playlist_id: int) -> Row:
        data = await self._request("GET", f"/api/mirrored-playlists/{playlist_id}")
        if (
            not integer(data.get("id"), 1)
            or data["id"] != playlist_id
            or data.get("source") != "soulsync"
            or not isinstance(data.get("tracks"), list)
            or not clean(data.get("display_name") or data.get("name"))
            or any(not isinstance(row, dict) for row in data["tracks"])
        ):
            raise SoulSyncError(INCOMPATIBLE)
        return data

    async def search(self, query: str) -> list[Row]:
        data = await self._request(
            "GET", f"/api/{self.source}/search_tracks", params={"query": query, "limit": 20}
        )
        rows = data.get("tracks")
        if not isinstance(rows, list):
            raise SoulSyncError(INCOMPATIBLE)
        return [track for row in rows[:20] if (track := normalize_track(row, self.source))]

    async def spotify(self, source_id: str) -> Preview:
        if not re.fullmatch(r"[A-Za-z0-9]{10,64}", source_id):
            raise SoulSyncError("Invalid Spotify playlist ID.")
        try:
            data = await self._request("GET", f"/api/spotify/playlist/{source_id}", long=True)
        except SoulSyncError as exc:
            if exc.status == 401:
                raise SoulSyncError(
                    "Check the SoulSync API key and connect Spotify in SoulSync to import playlists."
                ) from None
            if exc.status in {403, 404}:
                raise SoulSyncError(
                    "SoulSync could not open this public Spotify playlist."
                ) from None
            raise
        if data.get("public") is not True:
            raise SoulSyncError("Choose a public Spotify playlist.")
        expected = data.get("expected_total")
        if data.get("incomplete") is not False or (
            expected is not None and (not integer(expected) or expected != data.get("track_count"))
        ):
            raise SoulSyncError("SoulSync could not verify the full playlist. Try again later.")
        return self._playlist_preview(data, source_id, "spotify_public")

    async def deezer(self, source_id: str) -> Preview:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", source_id):
            raise SoulSyncError("Invalid Deezer playlist ID.")
        try:
            async with asyncio.timeout(self.import_timeout):
                data = await self._request(
                    "GET", f"/api/deezer/playlist/{source_id}", params={"async": "1"}, long=True
                )
                if data.get("pending") is True:
                    data = await self._deezer_playlist_job(data, source_id)
        except TimeoutError:
            raise SoulSyncError(
                "SoulSync took too long to load this Deezer playlist. Try again later."
            ) from None
        except SoulSyncError as exc:
            if exc.status == 404:
                raise SoulSyncError(
                    "SoulSync could not load this Deezer playlist. Check that it is public."
                ) from None
            raise
        return self._playlist_preview(data, source_id, "deezer")

    async def _deezer_playlist_job(self, initial: Row, source_id: str) -> Row:
        job = initial.get("job_id")
        if (
            not isinstance(job, str)
            or not re.fullmatch(r"deezer_playlist_[0-9a-f]{12}", job)
            or str(initial.get("playlist_id")) != source_id
        ):
            raise SoulSyncError(INCOMPATIBLE)
        while True:
            data = await self._request("GET", f"/api/deezer/playlist-load/{job}")
            if data.get("job_id") != job or str(data.get("playlist_id")) != source_id:
                raise SoulSyncError(INCOMPATIBLE)
            status = data.get("status")
            if status == "complete" and isinstance(data.get("playlist"), dict):
                return data["playlist"]
            if status == "error":
                raise SoulSyncError(
                    "SoulSync could not load this Deezer playlist. Check that it is public."
                )
            if status not in ("queued", "running"):
                raise SoulSyncError(INCOMPATIBLE)
            await asyncio.sleep(1.5)

    def _playlist_preview(self, data: Row, source_id: str, source: str) -> Preview:
        rows, count = data.get("tracks"), data.get("track_count")
        name = clean(data.get("name"))
        if str(data.get("id")) != source_id or not name or not isinstance(rows, list):
            raise SoulSyncError(INCOMPATIBLE)
        if not integer(count) or count != len(rows):
            raise SoulSyncError("SoulSync could not verify the full playlist. Try again later.")
        if len(rows) > self.max_tracks:
            raise SoulSyncError(f"This import exceeds the {self.max_tracks:,}-track limit.")
        tracks = [track for row in rows if (track := normalize_track(row, source, imported=True))]
        if not tracks:
            raise SoulSyncError("This playlist has no importable songs.")
        if len(json.dumps(tracks, ensure_ascii=False).encode()) > 2 * 1024**2:
            raise SoulSyncError("This playlist exceeds the import size limit.")
        return Preview(source_id, name, clean(data.get("owner")), tracks, len(rows) - len(tracks))

    async def create(self, name: str, tracks: list[Row] | None = None) -> Write:
        data = await self._request(
            "POST",
            "/api/user-playlists",
            body={"name": name, **({"tracks": tracks} if tracks else {})},
            long=bool(tracks),
            create_copy=bool(tracks),
        )
        identifier = data.get("id")
        if data.get("success") is not True or not integer(identifier, 1):
            raise SoulSyncError(INCOMPATIBLE, uncertain=True)
        return self._write(data, identifier, len(tracks)) if tracks else Write(identifier, 0, 0, 0)

    async def add(self, playlist_id: int, tracks: list[Row], *, importing: bool = False) -> Write:
        data = await self._request(
            "POST",
            f"/api/user-playlists/{playlist_id}/tracks",
            body={"tracks": tracks, "allow_duplicates": False},
            long=importing,
        )
        return self._write(data, playlist_id, len(tracks))

    @staticmethod
    def _write(data: Row, identifier: int, requested: int) -> Write:
        added, duplicates, count = (
            data.get("added"),
            data.get("duplicates"),
            data.get("track_count"),
        )
        if (
            data.get("success") is not True
            or not integer(added)
            or not integer(count)
            or not isinstance(duplicates, list)
            or added + len(duplicates) != requested
            or count < added
        ):
            raise SoulSyncError(INCOMPATIBLE, uncertain=True)
        return Write(identifier, added, len(duplicates), count)

    async def pipeline(self, playlist_id: int) -> Row:
        data = await self._request("GET", f"/api/mirrored-playlists/{playlist_id}/pipeline/status")
        return self._pipeline(data, playlist_id)

    async def run(self, playlist_id: int) -> Row:
        data = await self._request(
            "POST", f"/api/mirrored-playlists/{playlist_id}/pipeline/run", body={}
        )
        if data.get("success") is not True:
            raise SoulSyncError(INCOMPATIBLE, uncertain=True)
        state = self._pipeline(data.get("state"), playlist_id, uncertain=True)
        if state["status"] == "idle":
            raise SoulSyncError(INCOMPATIBLE, uncertain=True)
        return state

    @staticmethod
    def _pipeline(data: Any, identifier: int, *, uncertain: bool = False) -> Row:
        if (
            not isinstance(data, dict)
            or data.get("playlist_id") != identifier
            or data.get("status") not in ("idle", "running", "finished", "skipped", "error")
        ):
            raise SoulSyncError(INCOMPATIBLE, uncertain=uncertain)
        started = data.get("started_at")
        if data["status"] != "idle" and not finite(started):
            raise SoulSyncError(INCOMPATIBLE, uncertain=uncertain)
        progress = data.get("progress")
        return {
            "status": data["status"],
            "started_at": started,
            "run_id": clean(data.get("run_id")),
            "phase": clean(data.get("phase"))[:11],
            "progress": min(100, max(0, int(progress))) if finite(progress) else None,
            "result": {
                k: int(str(v))
                for k, v in (data.get("result") or {}).items()
                if k in {"tracks_synced", "sync_skipped", "wishlist_queued"}
                and re.fullmatch(r"[0-9]{1,9}", str(v))
            }
            if isinstance(data.get("result"), dict)
            else {},
        }
