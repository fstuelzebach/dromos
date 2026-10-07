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
