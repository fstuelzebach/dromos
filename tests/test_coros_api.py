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
    client.http = FakeSession([FakeResponse({}, status=500)] * 3)
    for _ in range(2):
        with pytest.raises(CorosError, match="request failed"):
            client.list_activities_page(1)
    with pytest.raises(CorosError, match="in a row"):
        client.list_activities_page(1)


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
