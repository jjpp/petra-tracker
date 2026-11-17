# SPDX-License-Identifier: GPL-3.0-or-later
"""Polite retry policy: exponential backoff with full jitter.

The server advertises `x-ratelimit-*` headers (express-rate-limit) and returns
429 + Retry-After when the budget is exceeded. The official app has no backoff
of its own, so we add one.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

LOW_REMAINING_THRESHOLD = 50


@dataclass
class RateLimitInfo:
    limit: int | None = None
    remaining: int | None = None
    reset_seconds: int | None = None

    @classmethod
    def from_headers(cls, headers: dict[str, str] | None) -> RateLimitInfo:
        def num(name: str) -> int | None:
            if not headers:
                return None
            raw = headers.get(name) or headers.get(name.title())
            try:
                return int(float(raw)) if raw is not None else None
            except ValueError:
                return None

        return cls(
            limit=num("x-ratelimit-limit"),
            remaining=num("x-ratelimit-remaining"),
            reset_seconds=num("x-ratelimit-reset"),
        )

    def throttle_delay(self) -> timedelta | None:
        """If we are close to the limit, wait for the window to reset."""
        if self.remaining is None or self.remaining >= LOW_REMAINING_THRESHOLD:
            return None
        # The server has been seen to report reset=0; fall back to a cautious minute.
        seconds = self.reset_seconds if self.reset_seconds and self.reset_seconds > 0 else 60
        return timedelta(seconds=min(seconds, 15 * 60))


@dataclass
class Backoff:
    base: timedelta = timedelta(seconds=30)
    cap: timedelta = timedelta(minutes=5)
    factor: float = 2.0
    rng: Callable[[], float] = field(default=random.random)
    failures: int = 0
    first_failure_at: datetime | None = None

    def failure(self, now: datetime, retry_after: timedelta | None = None) -> timedelta:
        """Register a failed attempt and return how long to wait."""
        self.failures += 1
        if self.first_failure_at is None:
            self.first_failure_at = now
        if retry_after is not None:
            return min(max(retry_after, timedelta(seconds=1)), self.cap * 3)
        ceiling = min(self.base.total_seconds() * self.factor ** (self.failures - 1), self.cap.total_seconds())
        # Full jitter, but never hammer faster than half the base interval.
        delay = max(ceiling * self.rng(), self.base.total_seconds() / 2)
        return timedelta(seconds=delay)

    def success(self) -> None:
        self.failures = 0
        self.first_failure_at = None

    def failing_for(self, now: datetime) -> timedelta:
        return now - self.first_failure_at if self.first_failure_at else timedelta(0)
