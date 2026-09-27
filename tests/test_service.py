from datetime import timedelta

import httpx
import pytest
from conftest import KEY, NOW, event, feed_xml

from illyriad_notifier.domain import Feed, Rule, ValidationError
from illyriad_notifier.service import Service
from illyriad_notifier.store import Store


def configure(service):
    service.save_settings(
        {
            "ntfy_url": "https://ntfy.test",
            "topic": "test-town",
            "delivery_enabled": True,
            "ntfy_token": "test-token",
        }
    )
    service.store.save_rule(Rule("Forward", action="forward", priority=4))


def test_initial_import_dedup_restart_and_new_rules(service, store):
    configure(service)
    assert service.ingest(Feed("42", (event(),)), NOW) == 1
    assert store.event("1")["status"] == "history"
    assert service.ingest(Feed("42", (event(),)), NOW) == 0
    restarted = Service(Store(store.directory), service.client)
    assert restarted.ingest(Feed("42", (event(), event("2"))), NOW) == 1
    assert store.event("2")["status"] == "pending"
    store.delete_rule(store.rules()[0].id)
    restarted.ingest(Feed("42", (event("3"),)), NOW)
    assert store.event("3")["status"] == "kept"
    assert store.event("2")["status"] == "pending"


def test_empty_feed_establishes_baseline(service, store):
    configure(service)
    service.ingest(Feed("42", ()), NOW)
    service.ingest(Feed("42", (event(),)), NOW)
    assert store.event("1")["status"] == "pending"


def test_other_player_is_rejected_atomically(service, store):
    service.ingest(Feed("42", (event(),)), NOW)
    with pytest.raises(ValidationError):
        service.ingest(Feed("43", (event("2"),)), NOW)
    assert store.event("2") is None
    assert store.metadata()["player_id"] == "42"


def test_retention_tombstones_and_pending_preserved(service, store):
    configure(service)
    service.ingest(Feed("42", (event(),)), NOW)
    service.ingest(Feed("42", (event("2"),)), NOW)
    service.prune(NOW + timedelta(days=91))
    assert store.event("1") is None
    assert store.event("2") is not None
    assert service.ingest(Feed("42", (event(),)), NOW + timedelta(days=92)) == 0
    assert store.event("1") is None


def test_delivery_snapshot_pause_and_success(store):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "accepted"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = Service(store, client)
        configure(service)
        service.ingest(Feed("42", ()), NOW)
        service.ingest(Feed("42", (event(),)), NOW)
        rule = store.rules()[0]
        store.delete_rule(rule.id)
        service.save_settings({"delivery_enabled": False})
        assert not service.deliver_one(NOW)
        service.save_settings({"delivery_enabled": True})
        assert service.deliver_one(NOW)
        assert not service.deliver_one(NOW)
    assert store.event("1")["status"] == "sent"
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert b'"priority":4' in requests[0].content
    assert b'"topic":"test-town"' in requests[0].content


def test_retry_exhaustion_manual_retry_and_no_early_delivery(store):
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as client:
        service = Service(store, client)
        configure(service)
        service.ingest(Feed("42", ()), NOW)
        service.ingest(Feed("42", (event(),)), NOW)
        assert service.deliver_one(NOW)
        assert not service.deliver_one(NOW + timedelta(seconds=59))
        for hour in range(1, 5):
            assert service.deliver_one(NOW + timedelta(hours=hour))
        item = store.event("1")
        assert item["status"] == "failed"
        assert len(item["deliveries"]) == 5
        assert not service.deliver_one(NOW + timedelta(days=1))
        assert service.retry("1", NOW + timedelta(days=1))
        assert service.deliver_one(NOW + timedelta(days=1))
        assert len(store.event("1")["deliveries"]) == 6
        assert not service.retry("1", NOW)


def test_poll_limit_survives_restart_and_error_redacted(store):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError(
            "private-url-and-key-should-not-escape", request=request
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = Service(store, client)
        service.save_settings({"api_key": KEY})
        assert "Could not reach" in service.poll(NOW)
        assert "private-url" not in str(store.metadata())
        assert "Next eligible" in Service(Store(store.directory), client).poll(
            NOW + timedelta(minutes=59)
        )
        assert len(calls) == 1
        service.poll(NOW + timedelta(hours=1))
        assert len(calls) == 2
        assert not store.metadata().get("initialized")


def test_successful_poll_and_no_redirect_follow(store):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=feed_xml())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = Service(store, client)
        service.save_settings({"api_key": KEY})
        assert "Imported 1" in service.poll(NOW)
        assert store.event("1")["status"] == "history"
    assert len(calls) == 1


def test_secrets_encrypted_and_blank_keeps_existing(service, store):
    service.save_settings({"api_key": KEY, "ntfy_token": "a-private-token"})
    with store.connection() as db:
        raw = db.execute("SELECT data FROM settings").fetchone()[0]
    assert KEY not in raw and "a-private-token" not in raw
    assert Store(store.directory).settings()["api_key"] == KEY
    service.save_settings({"api_key": "", "ntfy_token": ""})
    assert store.settings()["ntfy_token"] == "a-private-token"
    service.save_settings({"clear_ntfy_token": True})
    assert store.settings()["ntfy_token"] == ""


def test_settings_validation_is_atomic(service, store):
    before = store.settings()
    with pytest.raises(ValidationError):
        service.save_settings({"poll_minutes": 1, "api_key": KEY})
    assert store.settings() == before


def test_missing_encryption_key_does_not_replace_it(store):
    (store.directory / "secrets.json").unlink()
    with pytest.raises(RuntimeError, match="Restore"):
        Store(store.directory)


def test_redirect_does_not_leak_key_to_other_origin(store):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.test"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = Service(store, client)
        service.save_settings({"api_key": KEY})
        assert "HTTP 302" in service.poll(NOW)
    assert len(calls) == 1
    assert calls[0].url.host == "elgea.illyriad.co.uk"
    assert not store.metadata().get("initialized")


def test_token_rejects_invalid_header_characters(service):
    for token in ("abc\nsecret", "ab\x00cd", "token-é"):
        with pytest.raises(ValidationError):
            service.save_settings({"ntfy_token": token})
