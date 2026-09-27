"Bounded XML ingestion; no credential-bearing URLs escape this adapter."

import re
import xml.etree.ElementTree as ET

import httpx

from .domain import Feed, Notification, ValidationError, plain_text, timestamp

MAX_FEED_BYTES = 5_000_000


def parse_feed(data: bytes) -> Feed:
    if len(data) > MAX_FEED_BYTES:
        raise ValidationError("The notification feed exceeds the 5 MB limit.")
    try:
        xml = data.decode("utf-8-sig")
        if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise ValidationError("XML document types and entities are not supported.")
        root = ET.fromstring(xml)
        if root.tag != "notificationsapi":
            raise ValidationError(
                "Illyriad did not return a notification feed. Check the key."
            )
        player = root.find("player")
        player_id = player.get("id", "") if player is not None else ""
        if not player_id.isdigit() or root.find("notifications") is None:
            raise ValidationError("The feed is missing player or notification data.")
        events: list[Notification] = []
        for entry in root.findall("./notifications/notification"):
            values: dict[str, str] = {}
            labels: dict[str, str] = {}
            for tag in (
                "notification",
                "notificationtype",
                "notificationoveralltype",
                "notificationtown",
            ):
                node = entry.find(tag)
                value = node.get("id", "") if node is not None else ""
                if not re.fullmatch(r"\d{1,18}", value) and not (
                    tag == "notificationtown" and value == "-1"
                ):
                    raise ValidationError(
                        "A notification is missing a valid identifier."
                    )
                values[tag] = value
                labels[tag] = (
                    plain_text(node.text or "")[:100] if node is not None else ""
                )
            detail_node = entry.find("notificationdetail")
            if detail_node is None:
                raise ValidationError("A notification is missing its details.")
            # Support both escaped HTML and actual child elements.
            detail = (detail_node.text or "") + "".join(
                ET.tostring(child, encoding="unicode") for child in detail_node
            )
            events.append(
                Notification(
                    values["notification"],
                    values["notificationtype"],
                    values["notificationoveralltype"],
                    values["notificationtown"],
                    plain_text(detail),
                    timestamp(entry.findtext("notificationoccurrencedate", "")),
                    labels["notificationtype"],
                    labels["notificationoveralltype"],
                    labels["notificationtown"],
                )
            )
        return Feed(player_id, tuple(events))
    except (ET.ParseError, UnicodeDecodeError):
        raise ValidationError("Illyriad returned invalid UTF-8 XML.") from None


def fetch_feed(client: httpx.Client, key: str) -> Feed:
    try:
        with client.stream(
            "GET",
            "https://elgea.illyriad.co.uk/external/notificationsapi/" + key,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise ValidationError(f"Illyriad returned HTTP {response.status_code}.")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FEED_BYTES:
                    raise ValidationError(
                        "The notification feed exceeds the 5 MB limit."
                    )
        return parse_feed(bytes(data))
    except httpx.HTTPError:
        raise ValidationError(
            "Could not reach Illyriad. Check connectivity and retry later."
        ) from None
