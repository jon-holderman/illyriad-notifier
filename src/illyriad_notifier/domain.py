"""Notification normalization and deterministic rule matching."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit


class ValidationError(ValueError):
    """A safe, user-facing validation failure."""


@dataclass(frozen=True)
class Notification:
    id: str
    type_id: str
    category_id: str
    town_id: str
    detail: str
    occurred_at: str
    type_label: str = ""
    category_label: str = ""
    town_label: str = ""


@dataclass(frozen=True)
class Feed:
    player_id: str
    events: tuple[Notification, ...]


@dataclass(frozen=True)
class Rule:
    name: str
    position: int = 10
    enabled: bool = True
    type_ids: str = ""
    category_ids: str = ""
    town_ids: str = ""
    action: str = "keep"
    topic: str = ""
    priority: int = 3
    id: int = 0

    def matches(self, event: Notification) -> bool:
        return self.enabled and all(
            not values or actual in values.split(",")
            for values, actual in (
                (self.type_ids, event.type_id),
                (self.category_ids, event.category_id),
                (self.town_ids, event.town_id),
            )
        )


def match_rule(event: Notification, rules: list[Rule]) -> Rule | None:
    return next(
        (
            rule
            for rule in sorted(rules, key=lambda r: (r.position, r.id))
            if rule.matches(event)
        ),
        None,
    )


def ids(value: str) -> str:
    parts = list(dict.fromkeys(p.strip() for p in value.split(",") if p.strip()))
    if len(parts) > 100 or any(not re.fullmatch(r"-?\d{1,18}", p) for p in parts):
        raise ValidationError("Use comma-separated numeric IDs (up to 100).")
    return ",".join(parts)


def topic(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValidationError(
            "Topics need 1–64 letters, numbers, underscores or hyphens."
        )
    if value in {"app", "docs", "settings", "file", "v1", "account", "stats"}:
        raise ValidationError("That topic is reserved by ntfy.")
    return value


def validate_rule(rule: Rule) -> Rule:
    if not rule.name.strip() or len(rule.name) > 100:
        raise ValidationError("Give the rule a name of up to 100 characters.")
    if rule.action not in {"keep", "forward"}:
        raise ValidationError("Choose an inbox or forward action.")
    if not 1 <= rule.priority <= 5 or not 0 <= rule.position <= 9999:
        raise ValidationError("Priority must be 1–5 and order must be 0–9999.")
    return Rule(
        rule.name.strip(),
        rule.position,
        rule.enabled,
        ids(rule.type_ids),
        ids(rule.category_ids),
        ids(rule.town_ids),
        rule.action,
        topic(rule.topic),
        rule.priority,
        rule.id,
    )


def validate_server(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValidationError("The ntfy server URL is invalid.") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
        or any(c.isspace() for c in value)
    ):
        raise ValidationError(
            "Use an http(s) ntfy origin without a path or credentials."
        )
    try:
        _ = parsed.port
    except ValueError:
        raise ValidationError("The ntfy server port is invalid.") from None
    return value


def validate_key(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(r"elgea-NOTIF-[A-Za-z0-9_=-]{20,1000}", value):
        raise ValidationError("Enter the elgea-NOTIF API key, not the full URL.")
    return value


def timestamp(value: str) -> str:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("The feed contains an invalid event date.") from None
    # Illyriad's timezone-free game timestamps are interpreted as UTC.
    return (
        date.replace(tzinfo=UTC).isoformat()
        if date.tzinfo is None
        else date.astimezone(UTC).isoformat()
    )


class PlainText(HTMLParser):
    "Strip feed HTML, including active content, before storage or display."

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = PlainText()
    parser.feed(value)
    return "\n".join(
        line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
    )[:20000]
