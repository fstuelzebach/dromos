import json
import zipfile

import pytest
from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError

from dromos import garmin


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    def get_thing(self, *args):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(garmin.time, "sleep", lambda s: None)


def test_rate_limit_is_waited_out_then_succeeds():
    f = garmin.Fetcher(FakeClient([GarminConnectTooManyRequestsError("429"), {"ok": 1}]))
    assert f.get("x", "get_thing") == (garmin.FOUND, {"ok": 1})


def test_not_found_is_gone_and_none_is_found():
    f = garmin.Fetcher(FakeClient([GarminConnectNotFoundError("404"), None]))
    assert f.get("a", "get_thing") == (garmin.GONE, None)
    assert f.get("b", "get_thing") == (garmin.FOUND, None)


def test_repeated_errors_fail_item_not_run():
    f = garmin.Fetcher(FakeClient([RuntimeError("boom")] * garmin.MAX_ATTEMPTS))
    assert f.get("x", "get_thing") == (garmin.FAILED, None)
    assert f.failures == ["x"]


def test_verify_finds_missing_and_corrupt(tmp_path):
    lists = tmp_path / "activity_lists" / "s1"
    lists.mkdir(parents=True)
    (lists / "page_000000.json").write_text(json.dumps(
        [{"activityId": i, "startTimeLocal": "2024-01-0%d 07:00:00" % i} for i in (1, 2, 3)]))
    acts = tmp_path / "activities"
    acts.mkdir()
    with zipfile.ZipFile(acts / "1.zip", "w") as z:
        z.writestr("1_ACTIVITY.fit", b"x")
    (acts / "1.json").write_text("{}")
    (acts / "2.zip").write_bytes(b"not a zip")
    (acts / "2.json").write_text("{}")
    r = garmin.verify(tmp_path, wellness=False)
    assert (r.expected, r.ok) == (3, 1)
    assert r.corrupt == ["2.zip"]
    assert set(r.missing) == {"3.zip", "3.json"}
    assert not r.complete
    garmin.verify(tmp_path, fix=True, wellness=False)
    assert not (acts / "2.zip").exists()


class ExtrasClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        def call(aid):
            self.calls.append((method, aid))
            return []  # an empty answer is still an answer
        return call


ACTS = [
    {"activityId": 1, "activityType": {"typeKey": "yoga"}},
    {"activityId": 2, "activityType": {"typeKey": "strength_training"}},
    {"activityId": 3, "activityType": {"typeKey": "treadmill_running"}},
]


def test_extras_runs_first_and_only_matching_endpoints(tmp_path):
    c = ExtrasClient()
    garmin.download_extras(garmin.Fetcher(c), tmp_path, ACTS)
    assert [m for m, _ in c.calls[:5]] == [m for _, m in garmin.RUN_EXTRAS]
    assert {a for _, a in c.calls[:5]} == {"3"}
    assert c.calls[5:] == [("get_activity_exercise_sets", "2")]  # yoga: nothing
    assert (tmp_path / "activity_extras" / "weather" / "3.json").read_text() == "[]"


def test_extras_are_idempotent_and_verify_sees_gaps(tmp_path):
    lists = tmp_path / "activity_lists" / "s1"
    lists.mkdir(parents=True)
    (lists / "page_000000.json").write_text(json.dumps(ACTS))
    c = ExtrasClient()
    garmin.download_extras(garmin.Fetcher(c), tmp_path, ACTS)
    n = len(c.calls)
    garmin.download_extras(garmin.Fetcher(c), tmp_path, ACTS)
    assert len(c.calls) == n
    assert garmin.verify(tmp_path, wellness=False).extras_missing == {}
    (tmp_path / "activity_extras" / "gear" / "3.json").unlink()
    assert garmin.verify(tmp_path, wellness=False).extras_missing == {"gear": 1}


def _coros_raw(tmp_path):
    from dromos import coros  # noqa: F401
    lists = tmp_path / "activity_lists" / "s1"
    lists.mkdir(parents=True)
    acts = tmp_path / "activities"
    acts.mkdir()
    spec = {1: ("yoga", 50_000), 2: ("running", 50_000), 3: ("running", 100), 4: ("running", 60_000),
            5: ("strength_training", 30_000)}
    rows = []
    for i, (kind, size) in spec.items():
        rows.append({"activityId": i, "activityType": {"typeKey": kind},
                     "startTimeLocal": f"2024-01-0{i} 07:00:00"})
        with zipfile.ZipFile(acts / f"{i}.zip", "w") as z:
            z.writestr(f"{i}_ACTIVITY.fit", b"x" * size)
    (lists / "page_000000.json").write_text(json.dumps(rows))


def test_coros_plan_orders_runs_first_and_skips_small(tmp_path):
    from dromos import coros
    _coros_raw(tmp_path)
    r = coros.plan(tmp_path, max_files=1)
    assert [(b["group"], [c.activity_id for c in b["items"]]) for b in r["batches"]] == [
        ("running", [2]), ("running", [4]), ("strength", [5]), ("other", [1])]
    assert [s["activity_id"] for s in r["skipped"]] == [3]


def test_coros_prepare_writes_zips_and_uploaded_are_not_repeated(tmp_path):
    from dromos import coros
    _coros_raw(tmp_path)
    out = tmp_path / "out"
    coros.prepare(tmp_path, out)
    with zipfile.ZipFile(out / "coros_001_running.zip") as z:
        assert sorted(z.namelist()) == ["2.fit", "4.fit"]
    assert coros.mark_uploaded(out, "coros_001_running") == 2
    r = coros.prepare(tmp_path, out)
    assert [b["name"] for b in r["batches"]] == ["coros_002_strength", "coros_003_other"]
