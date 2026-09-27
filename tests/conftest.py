from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from illyriad_notifier.domain import Notification
from illyriad_notifier.service import Service
from illyriad_notifier.store import Store

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
PASSWORD = "test-password-long-enough"
KEY = "elgea-NOTIF-" + "a" * 30


def event(event_id="1", type_id="11", town_id="21", category_id="31", **kwargs):
    return Notification(
        event_id,
        type_id,
        category_id,
        town_id,
        "Building complete.",
        NOW.isoformat(),
        **kwargs,
    )


def feed_xml(event_id="1", detail="&lt;b&gt;Library&lt;/b&gt; complete", extra=""):
    return f'''<?xml version="1.0"?>
<notificationsapi>
<server>
<datagenerationdatetime>2026-09-27T12:00:00</datagenerationdatetime>
</server>
<player id="42"/>
<playerapikey id="never-store-this"/>
<notifications>
<notification>
<notification id="{event_id}"/>
<notificationtype id="11">Construction</notificationtype>
<notificationoveralltype id="31">City</notificationoveralltype>
<notificationtown id="21">Alderhaven</notificationtown>
<notificationdetail>{detail}</notificationdetail>
<notificationoccurrencedate>2026-09-27T11:00:00</notificationoccurrencedate>
</notification>
{extra}</notifications>
</notificationsapi>'''.encode()


@pytest.fixture
def store(tmp_path: Path):
    return Store(tmp_path / "data", PASSWORD)


@pytest.fixture
def service(store):
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=feed_xml()))
    ) as client:
        yield Service(store, client)
