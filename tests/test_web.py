import re

import httpx
import pytest
from conftest import KEY, NOW, PASSWORD, event, feed_xml
from fastapi.testclient import TestClient

from illyriad_notifier.domain import Feed
from illyriad_notifier.web import create_app


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


@pytest.fixture
def client(tmp_path):
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=feed_xml()))
    )
    app = create_app(tmp_path / "web", PASSWORD, worker=False, client=http)
    with TestClient(app) as client:
        yield client
    http.close()


def login(client):
    response = client.get("/login")
    response = client.post(
        "/login", data={"csrf": csrf(response), "password": PASSWORD}
    )
    assert response.status_code == 200
    return csrf(response)


def test_authentication_and_all_pages(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert (
        client.get("/settings", follow_redirects=False).headers["location"] == "/login"
    )
    assert client.get("/healthz").json() == {"status": "ok"}
    login(client)
    for path in ("/", "/rules", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"


def test_login_and_mutations_require_csrf(client):
    response = client.get("/login")
    assert (
        client.post("/login", data={"csrf": "bad", "password": PASSWORD}).status_code
        == 403
    )
    token = login(client)
    assert client.post("/settings", data={"csrf": "bad"}).status_code == 403
    assert client.post("/logout", data={"csrf": token}).url.path == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303
    assert response.status_code == 200


def test_login_rate_limit_persists(client):
    token = csrf(client.get("/login"))
    for _ in range(5):
        assert (
            client.post("/login", data={"csrf": token, "password": "wrong"}).status_code
            == 401
        )
    assert (
        client.post("/login", data={"csrf": token, "password": PASSWORD}).status_code
        == 429
    )


def test_settings_credentials_not_rendered_and_labels(client):
    token = login(client)
    response = client.post(
        "/settings",
        data={
            "csrf": token,
            "api_key": KEY,
            "ntfy_token": "secret-token",
            "ntfy_url": "http://ntfy.test",
            "topic": "test",
        },
    )
    assert response.status_code == 200
    assert KEY not in response.text and "secret-token" not in response.text
    assert 'value="secret-token"' not in response.text
    client.app.state.service.ingest(Feed("42", (event(),)), NOW)
    response = client.post(
        "/settings/labels",
        data={"csrf": token, "kind": "town", "source_id": "21", "label": "Capital"},
    )
    assert response.status_code == 200
    assert "Capital" in client.get("/").text


def test_rule_crud_preview_and_first_match(client):
    token = login(client)
    client.app.state.service.ingest(
        Feed("42", (event(), event("2", town_id="22"))), NOW
    )
    data = {
        "csrf": token,
        "name": "Capital only",
        "enabled": "true",
        "action": "forward",
        "town_ids": ["21"],
        "type_ids": ["11"],
        "priority": "4",
    }
    preview = client.post("/rules/preview", data=data)
    assert preview.status_code == 200
    assert "1 matches among the latest 2" in preview.text
    assert client.app.state.store.rules() == []
    assert client.post("/rules/save", data=data).status_code == 200
    rule = client.app.state.store.rules()[0]
    edit = client.get(f"/rules/{rule.id}/edit")
    assert edit.status_code == 200
    assert 'value="21" selected' in edit.text
    client.app.state.service.ingest(Feed("42", (event("3"),)), NOW)
    assert client.app.state.store.event("3")["status"] == "pending"
    assert (
        client.post(f"/rules/{rule.id}/delete", data={"csrf": token}).status_code == 200
    )
    assert client.app.state.store.rules() == []


def test_inbox_filters_read_state_and_xss_escape(client):
    token = login(client)
    from dataclasses import replace

    client.app.state.service.ingest(
        Feed(
            "42",
            (
                replace(event(), detail="<script>alert(1)</script>"),
                event("2", town_id="22"),
            ),
        ),
        NOW,
    )
    response = client.get("/?town=21")
    assert "/notifications/1" in response.text
    assert "/notifications/2" not in response.text
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    detail = client.get("/notifications/1")
    assert "<script>" not in detail.text
    client.post("/notifications/1/read", data={"csrf": token, "read": "true"})
    assert client.app.state.store.event("1")["is_read"] == 1
    assert "/notifications/1" not in client.get("/?unread=true").text
    assert client.get("/?since=not-a-date").status_code == 400
    assert client.get("/notifications/missing").status_code == 404


def test_password_change_revokes_existing_sessions(client):
    token = login(client)
    old_cookie = client.cookies.get("notifier_session")
    response = client.post(
        "/settings/password",
        data={
            "csrf": token,
            "current_password": PASSWORD,
            "new_password": "replacement-password",
            "confirm_password": "replacement-password",
        },
    )
    assert response.url.path == "/login"
    client.cookies.set("notifier_session", old_cookie)
    assert client.get("/settings", follow_redirects=False).status_code == 303


def test_validation_does_not_change_settings(client):
    token = login(client)
    response = client.post(
        "/settings", data={"csrf": token, "poll_minutes": "2", "api_key": KEY}
    )
    assert response.status_code == 400
    assert client.app.state.store.settings()["api_key"] == ""
