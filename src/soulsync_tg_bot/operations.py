import asyncio
import time
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .db import Database
from .soulsync import Row, SoulSync, SoulSyncError, clean, track_key


def keys(tracks: list[Row]) -> set[tuple[str, str]]:
    return {
        track_key(artist, title)
        for track in tracks
        if (artist := clean(track.get("artist_name"))) and (title := clean(track.get("track_name")))
    }


class Operations:
    def __init__(self, db: Database, api: SoulSync):
        self.db = db
        self.api = api
        self.create_lock = asyncio.Lock()
        self.playlist_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def execute(self, value: str) -> None:
        row = self.db.operation(value)
        if not row or row["status"] != "queued":
            return
        kind, data = row["kind"], row["data"]
        pid = data.get("playlist_id")
        lock = self.create_lock if pid is None else self.playlist_locks[pid]
        async with lock:
            current = self.db.operation(value)
            if not current or current["status"] != "queued":
                return
            before: Any
            try:
                if kind == "pipeline":
                    assert pid is not None
                    before = await self.api.pipeline(pid)
                    if before["status"] == "running":
                        raise SoulSyncError("This playlist already has a sync running in SoulSync.")
                elif pid is None:
                    before = [entry["id"] for entry in await self.api.playlists()]
                else:
                    before = sorted(keys((await self.api.detail(pid))["tracks"]))
            except SoulSyncError as exc:
                self.db.save(value, "failed", {"error": str(exc)})
                return

            self.db.save(value, "starting", {"before": before, "dispatch": time.time()})
            try:
                if kind == "pipeline":
                    assert pid is not None
                    state = await self.api.run(pid)
                    self.db.save(value, state["status"], {"pipeline": state})
                    return
                if pid is None:
                    result = await self.api.create(data["name"], data.get("tracks"))
                else:
                    result = await self.api.add(
                        pid, data["tracks"], importing=kind == "import_existing"
                    )
            except SoulSyncError as exc:
                if exc.uncertain:
                    await self.recover(value, wait=True)
                else:
                    self.db.save(value, "failed", {"error": str(exc)})
                return
            self.db.save(value, "written", {"result": asdict(result)})
            await self.refresh(value)

    async def refresh(self, value: str) -> bool:
        row = self.db.operation(value)
        if not row or row["status"] != "written":
            return False
        result = row["data"]["result"]
        try:
            detail = await self.api.detail(result["playlist_id"])
        except SoulSyncError:
            return False
        self.db.save(value, "succeeded", {"result": result | self._details(detail)})
        return True

    async def resume(self, row: Row) -> None:
        pid = row["playlist_id"]
        lock = self.create_lock if pid is None else self.playlist_locks[pid]
        async with lock:
            await self.recover(row["id"], wait=time.time() - row["data"]["dispatch"] < 90)

    @staticmethod
    def _details(detail: Row) -> Row:
        return {
            "playlist_id": detail["id"],
            "name": detail.get("display_name") or detail["name"],
            "track_count": len(detail["tracks"]),
        }

    async def recover(self, value: str, *, wait: bool = False) -> bool:
        row = self.db.operation(value)
        if not row:
            return False
        if row["status"] == "written":
            return await self.refresh(value)
        if row["status"] not in {"starting", "unknown"}:
            return False
        delays = (0, 1, 2, 4, 8, 15) if wait else (0,)
        deadline = asyncio.get_running_loop().time() + 30
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(max(0.1, deadline - asyncio.get_running_loop().time())):
                    if await self._reconcile(row):
                        return True
            except (SoulSyncError, TimeoutError):
                pass
            if asyncio.get_running_loop().time() >= deadline:
                break
        self.db.save(value, "unknown", {})
        return False

    async def _reconcile(self, row: Row) -> bool:
        data, pid, kind = row["data"], row["playlist_id"], row["kind"]
        if kind == "pipeline":
            state = await self.api.pipeline(pid)
            started = state["started_at"]
            prior = data["before"].get("started_at")
            # The server timestamp is only evidence when it follows our request
            # and differs from the previous run. The profile-scoped run ID is reused.
            if state["status"] != "idle" and started != prior and started >= data["dispatch"]:
                self.db.save(row["id"], state["status"], {"pipeline": state})
                return True
            return False
        if pid is None:
            candidates = [
                item for item in await self.api.playlists() if item["id"] not in data["before"]
            ]
            if len(candidates) != 1:
                return False
            (candidate,) = candidates
            requested = clean(data["name"])[:200]
            name = candidate["name"]
            if name != requested and not (
                name.startswith(requested + " (")
                and name[len(requested) + 2 : -1].isdigit()
                and name.endswith(")")
            ):
                return False
            pid = candidate["id"]
        detail = await self.api.detail(pid)
        wanted = keys(data.get("tracks", []))
        actual = keys(detail["tracks"])
        if row["playlist_id"] is None and wanted != actual:
            return False
        if not wanted <= actual:
            return False
        result = self._details(detail) | {"present": len(wanted)}
        self.db.save(row["id"], "succeeded", {"result": result})
        return True

    async def poll(self, value: str) -> None:
        row = self.db.operation(value)
        if not row or row["status"] != "running":
            return
        state = await self.api.pipeline(row["playlist_id"])
        prior = row["data"]["pipeline"]
        if state["status"] == "idle" or state["started_at"] != prior["started_at"]:
            self.db.save(value, "unknown", {"error": "SoulSync no longer reports this sync run."})
        elif state != prior:
            self.db.save(value, state["status"], {"pipeline": state})
