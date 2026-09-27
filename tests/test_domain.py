from dataclasses import replace

import pytest
from conftest import event, feed_xml

from illyriad_notifier.domain import (
    Rule,
    ValidationError,
    match_rule,
    plain_text,
    validate_key,
    validate_rule,
    validate_server,
)
from illyriad_notifier.feed import MAX_FEED_BYTES, parse_feed


def test_type_town_and_category_combinations():
    rule = Rule("Specific", type_ids="11,12", town_ids="21,22", category_ids="31")
    assert rule.matches(event())
    assert rule.matches(event(type_id="12", town_id="22"))
    assert not rule.matches(event(type_id="99"))
    assert not rule.matches(event(town_id="99"))
    assert not rule.matches(event(category_id="99"))
    assert not replace(rule, enabled=False).matches(event())


def test_first_match_and_tie_order():
    broad = Rule("Broad", action="forward", position=20, id=1)
    keep = Rule("Keep town", town_ids="21", position=10, id=2)
    assert match_rule(event(), [broad, keep]) == keep
    assert match_rule(event(town_id="99"), [broad, keep]) == broad
    assert match_rule(event(), [replace(broad, position=10), keep]).id == 1
    assert match_rule(event(), []) is None


def test_rule_validation():
    assert validate_rule(Rule(" Test ", type_ids="11, 12,11")).type_ids == "11,12"
    for rule in [
        Rule(""),
        Rule("x", type_ids="abc"),
        Rule("x", priority=8),
        Rule("x", topic="bad/topic"),
        Rule("x", action="delete"),
    ]:
        with pytest.raises(ValidationError):
            validate_rule(rule)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host",
        "https://host/path",
        "https://user:pass@host",
        "https://host?key=secret",
        "https://host/#frag",
        "https://host:bad",
    ],
)
def test_invalid_ntfy_urls(url):
    with pytest.raises(ValidationError):
        validate_server(url)


def test_private_ntfy_origin_and_key_validation():
    assert validate_server("http://192.168.1.10:8080/") == "http://192.168.1.10:8080"
    with pytest.raises(ValidationError):
        validate_key("https://host/some-key")


def test_real_schema_and_redaction():
    feed = parse_feed(feed_xml())
    assert feed.player_id == "42"
    entry = feed.events[0]
    assert (entry.type_id, entry.category_id, entry.town_id) == ("11", "31", "21")
    assert entry.type_label == "Construction"
    assert entry.detail == "Library complete"
    assert entry.occurred_at == "2026-09-27T11:00:00+00:00"
    assert "never-store-this" not in repr(feed)


def test_plain_text_removes_active_content():
    assert (
        plain_text(
            '<script>secret()</script><b>Hello</b><br>world<img src="https://tracker">'
        )
        == "Hello\nworld"
    )
    assert (
        parse_feed(feed_xml(detail="<p>Hello</p><p>World</p>")).events[0].detail
        == "Hello\nWorld"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"not XML",
        b"<error>secret key invalid</error>",
        b"<notificationsapi/>",
        b'<!DOCTYPE foo [<!ENTITY bar "x">]><notificationsapi/>',
        b"x" * (MAX_FEED_BYTES + 1),
    ],
)
def test_invalid_feeds_fail_closed(payload):
    with pytest.raises(ValidationError):
        parse_feed(payload)


def test_one_malformed_event_rejects_whole_feed():
    with pytest.raises(ValidationError):
        parse_feed(feed_xml(extra="<notification/>"))


def test_notifications_without_a_town_are_supported():
    feed = parse_feed(
        feed_xml().replace(b'notificationtown id="21"', b'notificationtown id="-1"')
    )
    assert feed.events[0].town_id == "-1"
    assert validate_rule(Rule("No town", town_ids="-1")).matches(feed.events[0])


def test_malformed_host_gets_safe_validation_error():
    with pytest.raises(ValidationError):
        validate_server("https://[not-an-ip")
