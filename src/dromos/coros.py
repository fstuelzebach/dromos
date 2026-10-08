"""Prepare the Garmin archive for import into COROS Training Hub (Activity List > Import Data).

Everything here is derived from `data/raw/` and written to `data/processed/coros_import/`,
so it can be deleted and rebuilt at any time. Raw files are only read.

What COROS accepts (reported by third-party guides and search summaries on 2026-10-07; the
official support pages were not readable, so treat as unverified until the first upload):
- FIT or TCX only, no GPX; a .zip of many files is allowed.
- Each file 20 KB .. 200 MB; a bigger single file or zip fails. Smaller files are rejected.
- Large imports can take an hour or more to show up.
Not known: max files per upload, whether yoga/padel/tennis modes are accepted, whether
re-uploading creates duplicates. That is why batches are small, ordered by value (runs first)
and tracked, so a rejected batch costs little and nothing is ever uploaded twice by accident.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dromos.garmin import _listed, _atomic_write, is_running, is_strength

MIN_BYTES = 20 * 1024            # COROS rejects smaller files as empty
MAX_BATCH_BYTES = 100 * 1024**2  # well under the 200 MB limit
MAX_BATCH_FILES = 100            # per-upload file limit is unknown; keep batches small

GROUPS = ("running", "strength", "other")  # upload order: the focus first


def group_of(activity: dict) -> str:
    return "running" if is_running(activity) else "strength" if is_strength(activity) else "other"


@dataclass
class Candidate:
    activity_id: int
    group: str
    type_key: str
    start: str
    fit_name: str
    size: int


def _fit_member(zip_path: Path) -> tuple[str, int] | None:
    try:
        with zipfile.ZipFile(zip_path) as z:
            for i in z.infolist():
                if i.filename.lower().endswith(".fit"):
                    return i.filename, i.file_size
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def plan(raw_dir: Path, uploaded: set[int] | None = None, max_files: int = MAX_BATCH_FILES,
         max_bytes: int = MAX_BATCH_BYTES, first_number: int = 1) -> dict:
    """Decide which activity goes into which batch. Pure function of the raw data (no writes)."""
    uploaded = uploaded or set()
    ready: list[Candidate] = []
    skipped: list[dict] = []
    for aid, a in sorted(_listed(raw_dir).items()):
        base = {"activity_id": aid, "type": a["activityType"]["typeKey"], "start": a.get("startTimeLocal", "")}
        if aid in uploaded:
            continue
        member = _fit_member(raw_dir / "activities" / f"{aid}.zip")
        if member is None:
            skipped.append({**base, "reason": "no readable FIT in raw zip"})
        elif member[1] < MIN_BYTES:
            skipped.append({**base, "reason": f"under 20 KB ({member[1]} bytes), COROS rejects it"})
        else:
            ready.append(Candidate(aid, group_of(a), base["type"], base["start"], member[0], member[1]))

    batches: list[dict] = []
    for group in GROUPS:
        items = sorted((c for c in ready if c.group == group), key=lambda c: c.start)
        cur: list[Candidate] = []
        size = 0
        for c in items + [None]:
            if c is None or (cur and (len(cur) >= max_files or size + c.size > max_bytes)):
                if cur:
                    batches.append({"group": group, "items": cur, "bytes": size})
                cur, size = [], 0
            if c is not None:
                cur.append(c)
                size += c.size
    for n, b in enumerate(batches, first_number):
        b["name"] = f"coros_{n:03d}_{b['group']}"
    return {"batches": batches, "skipped": skipped}


def state_path(out_dir: Path) -> Path:
    return out_dir / "uploaded.json"


def load_uploaded(out_dir: Path) -> dict[str, list[int]]:
    p = state_path(out_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def uploaded_ids(out_dir: Path) -> set[int]:
    return {i for ids in load_uploaded(out_dir).values() for i in ids}


def prepare(raw_dir: Path, out_dir: Path, **kw) -> dict:
    """(Re)build the batch zips and manifest. Batches whose activities are marked uploaded are
    left out, so a later run only prepares what is new. Old, not-yet-uploaded batch zips are
    replaced because batching can shift when activities are added."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("coros_*.zip"):
        old.unlink()
    result = plan(raw_dir, uploaded_ids(out_dir), first_number=len(load_uploaded(out_dir)) + 1, **kw)
    manifest = []
    for b in result["batches"]:
        path = out_dir / f"{b['name']}.zip"
        tmp = path.with_name(path.name + ".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for c in b["items"]:
                with zipfile.ZipFile(raw_dir / "activities" / f"{c.activity_id}.zip") as src:
                    out.writestr(f"{c.activity_id}.fit", src.read(c.fit_name))
        tmp.replace(path)
        b["zip_bytes"] = path.stat().st_size
        manifest.append({"batch": b["name"], "group": b["group"], "files": len(b["items"]),
                         "zip_bytes": b["zip_bytes"], "first": b["items"][0].start[:10],
                         "last": b["items"][-1].start[:10],
                         "activity_ids": [c.activity_id for c in b["items"]]})
    _atomic_write(out_dir / "manifest.json", json.dumps(
        {"built": datetime.now().isoformat(timespec="seconds"), "batches": manifest,
         "skipped": result["skipped"]}, indent=1).encode("utf-8"))
    return result


def mark_uploaded(out_dir: Path, batch: str) -> int:
    """Record that a batch was uploaded to COROS so it is never prepared again."""
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = next((b for b in manifest["batches"] if b["batch"] == batch), None)
    if entry is None:
        raise KeyError(batch)
    state = load_uploaded(out_dir)
    state[batch] = entry["activity_ids"]
    _atomic_write(state_path(out_dir), json.dumps(state, indent=1).encode("utf-8"))
    return len(entry["activity_ids"])
