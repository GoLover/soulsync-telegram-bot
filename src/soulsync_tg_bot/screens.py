import re
from dataclasses import dataclass, field
from html import escape
from typing import Any

from .soulsync import Row


def clip(value: Any, size: int = 100) -> str:
    text = " ".join(str(value or "").split())
    return text[: size - 1] + "…" if len(text) > size else text


def safe(value: Any, size: int = 100) -> str:
    return escape(clip(value, size), quote=False)


@dataclass(frozen=True)
class Button:
    label: str
    action: str
    data: Row = field(default_factory=dict)
    style: str | None = None


@dataclass(frozen=True)
class Screen:
    text: str
    rows: list[list[Button]] = field(default_factory=list)


def home() -> Screen:
    return Screen(
        "<b>SoulSync</b>\nCreate, fill, and sync shared playlists.",
        [
            [Button("My playlists", "list", style="primary")],
            [Button("Create playlist", "create")],
            [Button("Import playlist", "import")],
        ],
    )


def pager(page: int, total: int, kind: str, data: Row) -> list[Button]:
    buttons = []
    if page > 0:
        buttons.append(Button("‹ Previous", kind, data | {"page": page - 1}))
    if total > 1:
        buttons.append(Button(f"{page + 1} / {total}", "noop"))
    if page + 1 < total:
        buttons.append(Button("Next ›", kind, data | {"page": page + 1}))
    return buttons


def track_lines(tracks: list[Row], start: int = 0, *, metadata: bool = False) -> str:
    lines = []
    for index, track in enumerate(tracks, start + 1):
        artist, title = safe(track.get("artist_name"), 65), safe(track.get("track_name"), 85)
        line = f"{index}. {artist} — {title}"
        if metadata:
            duration = track.get("duration_ms") or 0
            minutes, seconds = divmod(duration // 1000, 60)
            info = " · ".join(
                filter(
                    None,
                    [
                        safe(track.get("album_name"), 70),
                        f"{minutes}:{seconds:02}" if duration else "",
                    ],
                )
            )
            if info:
                line += f"\n    {info}"
        lines.append(line)
    return "\n".join(lines)


def pipeline_phase(phase: Any) -> str:
    match = re.match(r"Phase ([1-4])/4", str(phase or ""))
    if match:
        return {
            "1": "Refreshing playlist",
            "2": "Identifying songs",
            "3": "Syncing with your server",
            "4": "Queuing missing songs",
        }[match[1]]
    return "Working in SoulSync"


def operation_result(row: Row) -> tuple[str, list[list[tuple[str, str]]]]:
    status, kind, data = row["status"], row["kind"], row["data"]
    result = data.get("result") or {}
    pid = result.get("playlist_id") or row["playlist_id"]
    name = safe(result.get("name") or data.get("name") or "Playlist")
    if status == "written" and row["playlist_id"] is None and pid:
        name = f"Playlist #{pid}"
    buttons: list[list[tuple[str, str]]] = []
    if status in {"queued", "starting"}:
        if kind.startswith("import_"):
            return "Copying playlist…", []
        return ("Starting sync &amp; download…" if kind == "pipeline" else "Saving playlist…"), []
    if kind == "pipeline":
        state = data.get("pipeline") or {}
        if status == "running":
            progress = state.get("progress")
            suffix = f" · {progress}%" if progress is not None else ""
            text = f"<b>{name}</b>\n{pipeline_phase(state.get('phase'))}{suffix}"
            if data.get("monitor_paused"):
                text += "\nConnection interrupted. Checking again automatically."
            return text, []
        titles = {
            "finished": "Sync &amp; download finished",
            "skipped": "Sync skipped",
            "error": "Sync failed. See SoulSync for details.",
        }
        text = titles.get(status, "")
        if text:
            labels = {
                "tracks_synced": "Synced",
                "sync_skipped": "Skipped",
                "wishlist_queued": "Queued for download",
            }
            counts = [f"{labels[key]} {value}" for key, value in state.get("result", {}).items()]
            text = f"<b>{name}</b>\n{text}" + ("\n" + " · ".join(counts) if counts else "")
            return text, [[("View playlist", "view")], [("Menu", "home")]]
    if status == "unknown":
        destination = f" in <b>{name}</b>" if pid else ""
        text = f"SoulSync didn’t confirm the result{destination}."
        if not pid:
            text += "\nCheck My playlists for a newly created playlist."
        if kind != "pipeline":
            buttons.append([("Check again", "check")])
    elif status == "failed":
        text = safe(data.get("error") or "SoulSync could not complete this action.", 300)
    else:
        if kind == "create":
            text = f"Created <b>{name}</b>."
        elif "present" in result:
            count = result["present"]
            text = (
                f"<b>{name}</b>\nAll {count:,} requested unique songs are present."
                if count
                else f"Found <b>{name}</b> in SoulSync."
            )
        elif kind == "add":
            text = f"{'Added to' if result.get('added') else 'Already in'} <b>{name}</b>."
        else:
            text = (
                f"<b>{name}</b>\n{result['added']:,} added · "
                f"{result['duplicates']:,} duplicate entries skipped"
            )
            if data.get("skipped"):
                text += f" · {data['skipped']:,} unavailable in the snapshot"
        if status == "written":
            text += "\nPlaylist details are temporarily unavailable."
            buttons.append([("Check details again", "check")])
        elif result.get("track_count") is not None:
            text += f"\n{result['track_count']:,} songs in playlist"
        if kind.startswith("import_"):
            buttons.append([("Import another", "import")])
        elif kind == "add":
            buttons.append([("Add another song", "search"), ("Back to results", "results")])
        elif kind == "create":
            buttons.append([("Add songs", "search")])
    if pid:
        buttons.insert(0, [("View playlist", "view")])
    buttons.append([("My playlists", "list"), ("Menu", "home")])
    return text, buttons
