"""Garmin Connect export via the `garminconnect` package.

Unofficial: uses the same endpoints as the Garmin Connect website.

Guarantees:
- Every successful response is written to disk immediately (temp file + rename), so the
  run can be killed at any moment without losing anything or leaving half-written files.
- A file that exists is never fetched or rewritten again. Re-running only fills gaps.
- Rate limits (429) are waited out with growing sleeps and the run carries on by itself.
- Everything that happens is logged to `data/raw/_logs/garmin_export.log`.
- `verify()` checks the archive against the activity list, so nothing silently goes missing.
"""
from __future__ import annotations

import json
import logging
import random
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from dromos.config import PROJECT_ROOT

log = logging.getLogger("dromos.garmin")

TOKEN_DIR = PROJECT_ROOT / "tokens" / "garmin"  # gitignored
PAGE_SIZE = 100

BASE_DELAY = (1.0, 2.0)          # seconds between requests, normal pace
MAX_DELAY = 10.0                 # pace never slows beyond this
RATE_LIMIT_WAITS = (60, 120, 300, 600, 900)  # sleeps after consecutive 429s; last one repeats
MAX_RATE_LIMIT_WAITS = 20        # ~4h of waiting in a row, then give up (re-run later)
MAX_ATTEMPTS = 5                 # per item, for non-429 errors
ERROR_BACKOFF = (5, 15, 45, 90)  # sleeps between those attempts
MAX_CONSECUTIVE_FAILED_ITEMS = 8  # items given up on in a row => something systemic, stop

FOUND, GONE, FAILED = "found", "gone", "failed"


class ExportAborted(RuntimeError):
    """Raised when continuing is pointless right now. Safe to re-run; progress is on disk."""


def setup_logging(raw_dir: Path) -> Path:
    """Log to console and to an append-only file in data/raw/_logs/."""
    log_dir = raw_dir / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "garmin_export.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("dromos")
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    for handler in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    return path


def login(email: str | None = None, password: str | None = None, prompt_mfa=None) -> Garmin:
    """Reuse the saved session; fall back to a credential login once and save it."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        client = Garmin(prompt_mfa=prompt_mfa)
        client.login(str(TOKEN_DIR))
        return client
    except (GarminConnectAuthenticationError, GarminConnectConnectionError, OSError):
        if not (email and password):
            raise
    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login(str(TOKEN_DIR))
    return client


# --- writing ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _write_json(path: Path, data) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def _gone_marker(path: Path) -> Path:
    return path.with_name(path.name + ".gone")


def _mark_gone(path: Path, reason: str) -> None:
    _atomic_write(_gone_marker(path), f"{datetime.now().isoformat(timespec='seconds')} {reason}\n".encode())


def _have(path: Path) -> bool:
    """Already accounted for: either fetched, or Garmin confirmed there is nothing."""
    return path.exists() or _gone_marker(path).exists()


# --- fetching --------------------------------------------------------------------------

class Fetcher:
    """Calls the client with pacing, rate-limit sleeps, retries and re-login."""

    def __init__(self, client: Garmin) -> None:
        self.client = client
        self.delay_scale = 1.0
        self.failed_in_a_row = 0
        self.failures: list[str] = []

    def _pause(self) -> None:
        time.sleep(random.uniform(*BASE_DELAY) * self.delay_scale)

    def _relogin(self) -> bool:
        try:
            self.client.login(str(TOKEN_DIR))
            log.info("session refreshed from saved tokens")
            return True
        except Exception as e:
            log.error("could not refresh session: %r", e)
            return False

    def get(self, label: str, method: str, *args, **kwargs):
        """Returns (FOUND, data) | (GONE, None) | (FAILED, None). Raises ExportAborted if systemic."""
        rate_limited = attempts = 0
        relogged = False
        while True:
            try:
                data = getattr(self.client, method)(*args, **kwargs)
            except GarminConnectTooManyRequestsError:
                rate_limited += 1
                if rate_limited > MAX_RATE_LIMIT_WAITS:
                    raise ExportAborted("rate-limited too long; wait a few hours and re-run") from None
                wait = RATE_LIMIT_WAITS[min(rate_limited, len(RATE_LIMIT_WAITS)) - 1]
                self.delay_scale = min(self.delay_scale * 1.5, MAX_DELAY / BASE_DELAY[1])
                log.warning("429 on %s (#%d): sleeping %ds, pace now x%.1f",
                            label, rate_limited, wait, self.delay_scale)
                time.sleep(wait)
                continue
            except GarminConnectNotFoundError:
                log.info("nothing on Garmin for %s (404)", label)
                self.failed_in_a_row = 0
                return GONE, None
            except GarminConnectAuthenticationError:
                if relogged or not self._relogin():
                    raise ExportAborted("session rejected; run `dromos garmin login`") from None
                relogged = True
                continue
            except Exception as e:  # network hiccup, 5xx, odd payloads ...
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    log.error("GIVING UP on %s after %d attempts: %r", label, attempts, e)
                    self.failures.append(label)
                    self.failed_in_a_row += 1
                    if self.failed_in_a_row >= MAX_CONSECUTIVE_FAILED_ITEMS:
                        raise ExportAborted(f"{self.failed_in_a_row} items failed in a row") from e
                    return FAILED, None
                wait = ERROR_BACKOFF[min(attempts, len(ERROR_BACKOFF)) - 1]
                log.warning("error on %s (attempt %d/%d): %r; retry in %ds",
                            label, attempts, MAX_ATTEMPTS, e, wait)
                time.sleep(wait)
                continue
            self.failed_in_a_row = 0
            self.delay_scale = max(1.0, self.delay_scale * 0.95)  # speed back up slowly
            self._pause()
            return FOUND, data


# --- activities ------------------------------------------------------------------------

def list_activities(f: Fetcher, raw_dir: Path) -> list[dict]:
    """Fetch the whole activity list fresh (offsets shift when new activities appear, so
    pages are never reused across runs). Saved as a timestamped snapshot. Newest first."""
    snap = raw_dir / "activity_lists" / datetime.now().strftime("%Y%m%d-%H%M%S")
    activities: list[dict] = []
    start = 0
    while True:
        status, page = f.get(f"activity list @{start}", "get_activities", start, PAGE_SIZE)
        if status != FOUND:
            raise ExportAborted(f"could not read activity list at offset {start}")
        _write_json(snap / f"page_{start:06d}.json", page)
        activities.extend(page)
        log.info("activity list: %d so far", len(activities))
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    unique = {a["activityId"]: a for a in activities}
    if len(unique) != len(activities):
        log.warning("activity list had %d duplicates (paging shifted); deduplicated", len(activities) - len(unique))
    log.info("activity list complete: %d activities (snapshot %s)", len(unique), snap.name)
    return list(unique.values())


def download_activities(f: Fetcher, raw_dir: Path, activities: list[dict]) -> None:
    """Original file (zip with .fit) + detail JSON per activity; each saved the moment it arrives."""
    out_dir = raw_dir / "activities"
    todo = [a for a in activities
            if not (_have(out_dir / f"{a['activityId']}.zip") and _have(out_dir / f"{a['activityId']}.json"))]
    log.info("activities: %d listed, %d already complete, %d to fetch",
             len(activities), len(activities) - len(todo), len(todo))
    for i, act in enumerate(todo, 1):
        aid = act["activityId"]
        original, detail = out_dir / f"{aid}.zip", out_dir / f"{aid}.json"
        if not _have(original):
            status, data = f.get(f"activity {aid} original", "download_activity", str(aid),
                                 Garmin.ActivityDownloadFormat.ORIGINAL)
            if status == FOUND:
                _atomic_write(original, data)
            elif status == GONE:
                _mark_gone(original, "no original file")
        if not _have(detail):
            status, data = f.get(f"activity {aid} detail", "get_activity", str(aid))
            if status == FOUND:
                _write_json(detail, data)
            elif status == GONE:
                _mark_gone(detail, "no detail")
        if i % 25 == 0 or i == len(todo):
            log.info("activities: %d/%d fetched this run (failures so far: %d)", i, len(todo), len(f.failures))


# --- per-activity extras ---------------------------------------------------------------

# (folder name, client method). Verified on 2026-10-07 against a run and a strength activity:
# all respond; power zones and gear come back as [] when there is nothing, which is saved as-is.
# Left out on purpose: get_activity_details (~300 KB per run, same stream as the FIT file),
# split summaries and HR-zone times (already in the activity list JSON).
RUN_EXTRAS = (
    ("splits", "get_activity_splits"),
    ("typed_splits", "get_activity_typed_splits"),
    ("weather", "get_activity_weather"),
    ("gear", "get_activity_gear"),
    ("power_zones", "get_activity_power_in_timezones"),
)
STRENGTH_EXTRAS = (("exercise_sets", "get_activity_exercise_sets"),)


def is_running(activity: dict) -> bool:
    return "run" in (activity.get("activityType") or {}).get("typeKey", "")


def is_strength(activity: dict) -> bool:
    return (activity.get("activityType") or {}).get("typeKey") == "strength_training"


def extras_for(activity: dict) -> tuple[tuple[str, str], ...]:
    if is_running(activity):
        return RUN_EXTRAS
    if is_strength(activity):
        return STRENGTH_EXTRAS
    return ()


def download_extras(f: Fetcher, raw_dir: Path, activities: list[dict]) -> None:
    """Splits, weather, gear ... as one JSON per activity and kind. Runs first (the focus),
    then strength; other sports have no extras yet. Each file is saved the moment it arrives."""
    out = raw_dir / "activity_extras"
    ordered = [a for a in activities if is_running(a)] + [a for a in activities if is_strength(a)]
    todo = [(a, name, method) for a in ordered for name, method in extras_for(a)
            if not _have(out / name / f"{a['activityId']}.json")]
    log.info("extras: %d requests to make for %d activities", len(todo), len(ordered))
    for i, (act, name, method) in enumerate(todo, 1):
        aid = act["activityId"]
        path = out / name / f"{aid}.json"
        status, data = f.get(f"{name} {aid}", method, str(aid))
        if status == FOUND:
            _write_json(path, data)
        elif status == GONE:
            _mark_gone(path, "404")
        if i % 100 == 0 or i == len(todo):
            log.info("extras: %d/%d fetched this run (failures so far: %d)", i, len(todo), len(f.failures))


# --- wellness --------------------------------------------------------------------------

# (folder name, client method): one JSON file per day and kind.
DAILY = (
    ("sleep", "get_sleep_data"),
    ("hrv", "get_hrv_data"),
    ("stats", "get_stats"),
    ("training_status", "get_training_status"),
    ("max_metrics", "get_max_metrics"),
)


def download_wellness(f: Fetcher, raw_dir: Path, since: date, until: date | None = None) -> None:
    """Per-day wellness JSON, newest first. A 'no data that day' answer (null) is saved as
    `null` so the day counts as done and is not asked for again."""
    until = until or date.today()
    day, fetched = until, 0
    while day >= since:
        iso = day.isoformat()
        for name, method in DAILY:
            path = raw_dir / "wellness" / name / f"{iso}.json"
            if _have(path):
                continue
            status, data = f.get(f"{name} {iso}", method, iso)
            if status == FOUND:
                _write_json(path, data)
                fetched += 1
            elif status == GONE:
                _mark_gone(path, "404")
        if day.day == 1:
            log.info("wellness: reached %s (%d files fetched this run, failures so far: %d)",
                     iso, fetched, len(f.failures))
        day -= timedelta(days=1)
    log.info("wellness done: %d files fetched this run", fetched)


def earliest_activity_date(activities: list[dict]) -> date | None:
    days = [a["startTimeLocal"][:10] for a in activities if a.get("startTimeLocal")]
    return date.fromisoformat(min(days)) if days else None


# --- verification ----------------------------------------------------------------------

@dataclass
class Report:
    expected: int = 0
    ok: int = 0
    gone: int = 0
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    partial_files: list[str] = field(default_factory=list)
    wellness_missing: dict[str, int] = field(default_factory=dict)
    extras_missing: dict[str, int] = field(default_factory=dict)
    first_day: str | None = None
    last_day: str | None = None

    @property
    def complete(self) -> bool:
        return not (self.missing or self.corrupt or self.partial_files
                    or any(self.wellness_missing.values()) or any(self.extras_missing.values()))


def _listed(raw_dir: Path) -> dict[int, dict]:
    """Every activity ever listed (all snapshots, incl. the first run's loose pages)."""
    acts: dict[int, dict] = {}
    for page in (raw_dir / "activity_lists").rglob("page_*.json"):
        for a in json.loads(page.read_text(encoding="utf-8")):
            acts[a["activityId"]] = a
    return acts


def _listed_ids(raw_dir: Path) -> dict[int, str]:
    """Every activity id ever listed -> start date."""
    return {aid: (a.get("startTimeLocal") or "")[:10] for aid, a in _listed(raw_dir).items()}


def _zip_ok(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None and any(n.lower().endswith(".fit") for n in z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def _json_ok(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (ValueError, OSError):
        return False


def verify(raw_dir: Path, fix: bool = False, wellness: bool = True) -> Report:
    """Check the archive against the activity lists. With fix=True, corrupt files are deleted
    so the next export run fetches them again."""
    r = Report()
    ids = _listed_ids(raw_dir)
    r.expected = len(ids)
    out = raw_dir / "activities"
    for aid in sorted(ids):
        zip_p, json_p = out / f"{aid}.zip", out / f"{aid}.json"
        bad = False
        for p, check in ((zip_p, _zip_ok), (json_p, _json_ok)):
            if p.exists():
                if not check(p):
                    r.corrupt.append(p.name)
                    bad = True
                    if fix:
                        p.unlink()
            elif _gone_marker(p).exists():
                r.gone += 1
            else:
                r.missing.append(p.name)
                bad = True
        if not bad:
            r.ok += 1
    r.partial_files = [str(p.relative_to(raw_dir)) for p in raw_dir.rglob("*.part")]

    if (raw_dir / "activity_extras").exists():
        for a in _listed(raw_dir).values():
            for name, _ in extras_for(a):
                if not _have(raw_dir / "activity_extras" / name / f"{a['activityId']}.json"):
                    r.extras_missing[name] = r.extras_missing.get(name, 0) + 1

    dated = sorted(d for d in ids.values() if d)
    if dated:
        r.first_day, r.last_day = dated[0], dated[-1]
    if wellness and dated and (raw_dir / "wellness").exists():
        day, end = date.fromisoformat(dated[0]), date.today()
        for name, _ in DAILY:
            folder = raw_dir / "wellness" / name
            missing, d = 0, day
            while d <= end:
                if not _have(folder / f"{d.isoformat()}.json"):
                    missing += 1
                d += timedelta(days=1)
            r.wellness_missing[name] = missing
    return r


def log_report(r: Report) -> None:
    log.info("VERIFY activities: %d listed, %d complete, %d with a 404 marker, %d missing files, %d corrupt",
             r.expected, r.ok, r.gone, len(r.missing), len(r.corrupt))
    log.info("VERIFY activity date range: %s .. %s", r.first_day, r.last_day)
    for name in r.missing[:20]:
        log.warning("VERIFY missing: %s", name)
    for name in r.corrupt[:20]:
        log.warning("VERIFY corrupt: %s", name)
    for name in r.partial_files:
        log.warning("VERIFY leftover partial file: %s", name)
    for kind, n in r.wellness_missing.items():
        log.info("VERIFY wellness %-16s days still missing: %d", kind, n)
    for kind, n in r.extras_missing.items():
        log.info("VERIFY extras %-16s activities still missing: %d", kind, n)
    log.info("VERIFY result: %s", "COMPLETE" if r.complete else "INCOMPLETE; re-run the export to fill gaps")
