# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.petra_tracker.core import jwt
from custom_components.petra_tracker.core.backoff import Backoff, RateLimitInfo
from custom_components.petra_tracker.core.telemetry import REDACTED, TelemetryWriter, redact

from .. import synthetic as syn

NOW = datetime(2030, 1, 15, 7, 0, tzinfo=UTC)


def test_backoff_grows_caps_and_resets():
    b = Backoff(base=timedelta(seconds=30), cap=timedelta(minutes=5), rng=lambda: 1.0)
    delays = [b.failure(NOW).total_seconds() for _ in range(6)]
    assert delays == [30, 60, 120, 240, 300, 300]
    assert b.failing_for(NOW + timedelta(minutes=4)) == timedelta(minutes=4)
    b.success()
    assert b.failures == 0 and b.failing_for(NOW) == timedelta(0)


def test_backoff_jitter_has_floor_and_honours_retry_after():
    b = Backoff(base=timedelta(seconds=30), rng=lambda: 0.0)
    assert b.failure(NOW) == timedelta(seconds=15)  # never faster than base/2
    assert b.failure(NOW, retry_after=timedelta(seconds=90)) == timedelta(seconds=90)


def test_rate_limit_headers():
    info = RateLimitInfo.from_headers(
        {"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "999", "x-ratelimit-reset": "0"}
    )
    assert (info.limit, info.remaining) == (1000, 999)
    assert info.throttle_delay() is None
    low = RateLimitInfo(limit=1000, remaining=3, reset_seconds=0)
    assert low.throttle_delay() == timedelta(seconds=60)  # reset=0 seen in the wild
    assert RateLimitInfo(remaining=3, reset_seconds=120).throttle_delay() == timedelta(seconds=120)


def test_jwt_decode_and_normalise():
    token = syn.make_token(user_id=1234, iat=NOW, days=30)
    info = jwt.decode(f"JWT {token}")
    assert info.user_id == 1234
    assert info.expires_at - info.issued_at == timedelta(days=30)
    assert jwt.normalise_token(f"Authorization: JWT {token}") == token
    with pytest.raises(jwt.InvalidToken):
        jwt.decode("not-a-token")
    with pytest.raises(jwt.InvalidToken):
        jwt.decode("a.b.c")


def test_redact_keeps_structure_drops_personal_values():
    o = syn.order()
    clean = redact(o)
    assert set(clean) == set(o)
    assert clean["firstName"] == REDACTED and clean["phone"] == REDACTED
    assert clean["addresses"][0]["fullAddress"] == REDACTED
    assert clean["addresses"][0]["coordinates"] == o["addresses"][0]["coordinates"]
    assert clean["email"] == ""  # empty values stay empty (shows the field was empty)
    assert clean["bookRefNo"] == o["bookRefNo"]


def test_telemetry_dedupes_and_prunes(tmp_path):
    w = TelemetryWriter(tmp_path, retention_days=7)
    data = syn.order_list(syn.order())
    assert w.record(NOW, "list", "today", data, http_status=200)
    assert not w.record(NOW + timedelta(seconds=30), "list", "today", data)
    data["items"][0]["combinedStatus"] = "arriving"
    assert w.record(NOW + timedelta(seconds=60), "list", "today", data)
    lines = (tmp_path / "2030-01-15" / "list-today.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["data"]["items"][0]["firstName"] == REDACTED

    (tmp_path / "2029-12-01").mkdir()
    (tmp_path / "not-a-date").mkdir()
    assert w.prune(date(2030, 1, 15)) == 1
    assert (tmp_path / "not-a-date").exists() and (tmp_path / "2030-01-15").exists()
