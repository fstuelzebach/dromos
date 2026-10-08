"""Unofficial COROS Training Hub client (same endpoints as t.coros.com, no official API exists).

Endpoints (from community projects, NOT yet verified against a live account; see ROADMAP T6/T8):
  POST /account/login                  {"account", "accountType": 2, "pwd": md5(password)} -> data.accessToken
  GET  /activity/query                 size, pageNumber, startDay, endDay (YYYYMMDD), modeList -> data.dataList
  GET  /activity/detail/download       labelId, sportType, fileType -> data.fileUrl (then fetch that URL)
Token goes in the `accesstoken` header. Logging in on the website can invalidate a saved token.

Credentials come from .env (COROS_EMAIL, COROS_PASSWORD, COROS_REGION); the session token is cached in
tokens/coros/ (gitignored). Requests are throttled and stop after repeated errors.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from dromos.config import PROJECT_ROOT

log = logging.getLogger("dromos.coros")

TOKEN_FILE = PROJECT_ROOT / "tokens" / "coros" / "session.json"  # gitignored
REGIONS = {
    "eu": "https://teameuapi.coros.com",
    "us": "https://teamapi.coros.com",
    "cn": "https://teamcnapi.coros.com",
}
FIT_FILE_TYPE = 4  # as used by community clients; unverified
THROTTLE_SECONDS = 1.5
MAX_CONSECUTIVE_ERRORS = 3


class CorosError(RuntimeError):
    """Login failed, token rejected, or too many errors in a row."""


def credentials() -> tuple[str | None, str | None, str]:
    load_dotenv()
    region = (os.getenv("COROS_REGION") or "eu").lower()
    if region not in REGIONS:
        raise CorosError(f"COROS_REGION must be one of {sorted(REGIONS)}, got {region!r}")
    return os.getenv("COROS_EMAIL") or None, os.getenv("COROS_PASSWORD") or None, region


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class CorosClient:
    def __init__(self, region: str = "eu", token: str | None = None, throttle: float = THROTTLE_SECONDS):
        self.base = REGIONS[region]
        self.region = region
        self.token = token
        self.throttle = throttle
        self._last = 0.0
        self._errors = 0
        self.http = requests.Session()

    # --- session ------------------------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        body = {"account": email, "accountType": 2, "pwd": _md5(password)}
        data = self._request("POST", "/account/login", json=body, auth=False)
        token = (data.get("data") or {}).get("accessToken")
        if not token:
            # Message only; never echo the request body (it holds the credentials).
            raise CorosError(f"login rejected: {data.get('message') or data.get('result')}")
        self.token = token
        self._save_token()
        log.info("COROS login ok (region %s), token saved", self.region)

    def _save_token(self) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps({"region": self.region, "token": self.token}), encoding="utf-8")

    @classmethod
    def from_saved(cls, region: str) -> "CorosClient | None":
        try:
            saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if saved.get("region") != region or not saved.get("token"):
            return None
        return cls(region, token=saved["token"])

    @classmethod
    def connect(cls) -> "CorosClient":
        """Reuse the saved token if it still works, otherwise log in with .env credentials."""
        email, password, region = credentials()
        client = cls.from_saved(region)
        if client:
            try:
                client.list_activities_page(1, size=1)
                return client
            except CorosError:
                log.info("saved COROS token no longer valid, logging in again")
        if not (email and password):
            raise CorosError("set COROS_EMAIL and COROS_PASSWORD in .env (see .env.example)")
        client = cls(region)
        client.login(email, password)
        return client

    # --- requests -----------------------------------------------------------------------

    def _request(self, method: str, path: str, *, auth: bool = True, **kw) -> dict:
        if auth and not self.token:
            raise CorosError("not logged in")
        wait = self.throttle - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        headers = {"accesstoken": self.token} if auth else {}
        try:
            resp = self.http.request(method, self.base + path, headers=headers, timeout=30, **kw)
            self._last = time.monotonic()
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            self._errors += 1
            if self._errors >= MAX_CONSECUTIVE_ERRORS:
                raise CorosError(f"{self._errors} errors in a row, stopping: {type(e).__name__}") from None
            raise CorosError(f"request failed: {type(e).__name__}") from None
        self._errors = 0
        # COROS answers HTTP 200 with result != "0000" for logical errors such as an invalid token.
        if auth and data.get("result") not in (None, "0000"):
            raise CorosError(f"{path}: {data.get('message') or data.get('result')}")
        return data

    # --- activities ---------------------------------------------------------------------

    def list_activities_page(self, page: int, size: int = 100, start: date | None = None,
                             end: date | None = None) -> dict:
        params = {"size": size, "pageNumber": page, "modeList": ""}
        if start:
            params["startDay"] = start.strftime("%Y%m%d")
        if end:
            params["endDay"] = end.strftime("%Y%m%d")
        return self._request("GET", "/activity/query", params=params)["data"]

    def list_activities(self, start: date | None = None, end: date | None = None, size: int = 100):
        """Yield every activity row, page by page."""
        page = 1
        while True:
            data = self.list_activities_page(page, size, start, end)
            yield from data.get("dataList") or []
            if page >= int(data.get("totalPage") or 1):
                return
            page += 1

    def download_fit(self, label_id: str, sport_type: int) -> bytes:
        """Fetch the original activity file; returned unchanged for data/raw/."""
        params = {"labelId": label_id, "sportType": sport_type, "fileType": FIT_FILE_TYPE}
        info = self._request("POST", "/activity/detail/download", params=params)
        url = (info.get("data") or {}).get("fileUrl")
        if not url:
            raise CorosError(f"no file url for activity {label_id}")
        resp = self.http.get(url, timeout=60)
        self._last = time.monotonic()
        resp.raise_for_status()
        return resp.content


# --- export to data/raw/coros/ ---------------------------------------------------------------

# COROS sportType codes seen in the archive: 100/101 run types, 402 strength.
EXPORT_GROUPS = {"running": (100, 101), "strength": (402,)}


def _safe_id(label_id) -> str:
    text = str(label_id)
    if not text.isalnum():
        raise CorosError(f"unexpected activity id {text!r}")
    return text


def export_activities(client: CorosClient, raw_dir: Path, groups: list[str], limit: int | None = None) -> dict:
    """Save the original FIT file and the list row of each activity to raw_dir/coros/.

    Idempotent: an activity with a .fit on disk is skipped. The .json row is written after the .fit,
    so a missing .json means the download was interrupted (the next run fetches both again).
    Groups run in the given order. Returns counts; raises CorosError after repeated errors.
    """
    from dromos.garmin import _atomic_write, _write_json

    out = raw_dir / "coros"
    wanted = {t: g for g in groups for t in EXPORT_GROUPS[g]}
    rows = [r for r in client.list_activities() if r.get("sportType") in wanted]
    order = {g: i for i, g in enumerate(groups)}
    rows.sort(key=lambda r: (order[wanted[r["sportType"]]], r.get("startTime") or 0))
    done = skipped = 0
    for row in rows:
        aid = _safe_id(row["labelId"])
        fit, meta = out / f"{aid}.fit", out / f"{aid}.json"
        if fit.exists() and meta.exists():
            skipped += 1
            continue
        if limit is not None and done >= limit:
            break
        _atomic_write(fit, client.download_fit(aid, row["sportType"]))
        _write_json(meta, row)
        done += 1
        if done % 25 == 0:
            log.info("exported %d (of %d to do)", done, len(rows) - skipped)
    return {"downloaded": done, "skipped": skipped, "matching": len(rows)}
