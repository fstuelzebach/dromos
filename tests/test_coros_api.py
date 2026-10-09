"""Offline tests for the COROS client: HTTP is faked, no network and no real credentials."""
import hashlib

import pytest

from dromos import coros_api
from dromos.coros_api import CorosClient, CorosError


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.content = b"FIT"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise coros_api.requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)

    get = lambda self, url, **kw: self.request("GET", url, **kw)  # noqa: E731


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(coros_api, "TOKEN_FILE", tmp_path / "session.json")
    monkeypatch.setattr(coros_api.time, "sleep", lambda s: None)
    return CorosClient("eu", token="tok", throttle=0)


def test_login_sends_md5_and_saves_token(client):
    client.token = None
    client.http = FakeSession([FakeResponse({"result": "0000", "data": {"accessToken": "abc"}})])
    client.login("a@b.c", "secret")
    _, url, kw = client.http.calls[0]
    assert url.endswith("/account/login")
    assert kw["json"]["pwd"] == hashlib.md5(b"secret").hexdigest()
    assert client.token == "abc"
    assert "abc" in coros_api.TOKEN_FILE.read_text()


def test_login_rejected_does_not_leak_credentials(client):
    client.token = None
    client.http = FakeSession([FakeResponse({"result": "1030", "message": "wrong"})])
    with pytest.raises(CorosError) as e:
        client.login("a@b.c", "secret")
    assert "secret" not in str(e.value) and "a@b.c" not in str(e.value)


def test_list_activities_paginates(client):
    pages = [
        FakeResponse({"result": "0000", "data": {"dataList": [{"labelId": "1"}], "totalPage": 2}}),
        FakeResponse({"result": "0000", "data": {"dataList": [{"labelId": "2"}], "totalPage": 2}}),
    ]
    client.http = FakeSession(pages)
    assert [a["labelId"] for a in client.list_activities()] == ["1", "2"]
    assert client.http.calls[0][2]["headers"] == {"accesstoken": "tok"}


def test_invalid_token_result_raises(client):
    client.http = FakeSession([FakeResponse({"result": "1019", "message": "token invalid"})])
    with pytest.raises(CorosError):
        client.list_activities_page(1)


def test_stops_after_repeated_errors(client):
    client.http = FakeSession([FakeResponse({}, status=500)] * 9)
    for _ in range(2):
        with pytest.raises(CorosError, match="request failed"):
            client.list_activities_page(1)
    with pytest.raises(CorosError, match="in a row"):
        client.list_activities_page(1)


def test_transient_error_is_retried(client):
    client.http = FakeSession([FakeResponse({}, status=502),
                               FakeResponse({"result": "0000", "data": {"dataList": []}})])
    assert client.list_activities_page(1) == {"dataList": []}


def test_download_fit_two_step(client):
    client.http = FakeSession([
        FakeResponse({"result": "0000", "data": {"fileUrl": "https://files.example/x.fit"}}),
        FakeResponse(None),
    ])
    assert client.download_fit("1", 100) == b"FIT"


def test_region_validation(monkeypatch):
    monkeypatch.setenv("COROS_REGION", "mars")
    monkeypatch.setattr(coros_api, "load_dotenv", lambda: None)
    with pytest.raises(CorosError):
        coros_api.credentials()


def test_export_is_idempotent_and_runs_first(client, tmp_path):
    rows = [
        {"labelId": "s1", "sportType": 402, "startTime": 1},
        {"labelId": "r2", "sportType": 100, "startTime": 3},
        {"labelId": "r1", "sportType": 101, "startTime": 2},
        {"labelId": "y1", "sportType": 999, "startTime": 0},
    ]
    client.list_activities = lambda *a, **k: iter(rows)
    fetched = []
    client.download_fit = lambda aid, st: fetched.append(aid) or b"FIT" + aid.encode()

    first = coros_api.export_activities(client, tmp_path, ["running", "strength"])
    assert fetched == ["r1", "r2", "s1"]
    assert first == {"downloaded": 3, "skipped": 0, "matching": 3}
    assert (tmp_path / "coros" / "r1.fit").read_bytes() == b"FITr1"

    fetched.clear()
    second = coros_api.export_activities(client, tmp_path, ["running", "strength"])
    assert fetched == [] and second["skipped"] == 3


def test_export_limit_and_interrupted_download_is_retried(client, tmp_path):
    rows = [{"labelId": f"r{i}", "sportType": 100, "startTime": i} for i in range(3)]
    client.list_activities = lambda *a, **k: iter(rows)
    client.download_fit = lambda aid, st: b"x"
    assert coros_api.export_activities(client, tmp_path, ["running"], limit=1)["downloaded"] == 1
    (tmp_path / "coros" / "r0.json").unlink()  # fit without row = interrupted
    assert coros_api.export_activities(client, tmp_path, ["running"])["downloaded"] == 3


def test_export_rejects_odd_ids(client, tmp_path):
    client.list_activities = lambda *a, **k: iter([{"labelId": "../x", "sportType": 100}])
    with pytest.raises(CorosError):
        coros_api.export_activities(client, tmp_path, ["running"])
