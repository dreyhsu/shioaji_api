"""Offline, clock-injected recovery policy. Never logs in or submits orders."""
from __future__ import annotations

from dataclasses import dataclass
import random
import re
import sqlite3
import time


def error_kind(error: BaseException) -> str:
    """Inspect both exception links, preserving DB/auth precedence over text."""
    pending, seen, kinds = [error], set(), set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        text = f"{type(current).__name__} {current}".lower()
        if isinstance(current, sqlite3.Error):
            kinds.add("database")
        elif re.search(r"\b(unauthorized|authenticationfailed|invalidtoken|tokenexpired)\b|response\s*code\s*[:=]\s*401", text):
            kinds.add("authentication")
        elif re.search(r"\b(sessionnotestablished|notready|sessiondownerror)\b", text):
            kinds.add("session")
        elif re.search(r"\b(quotaexceeded|quota exceeded|data usage exceeded|usage limit exceeded)\b", text):
            kinds.add("quota")
        elif isinstance(current, (TimeoutError, ConnectionError, OSError)):
            kinds.add("transport")
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return next((kind for kind in ("database", "authentication", "session", "quota", "transport") if kind in kinds), "other")


@dataclass(frozen=True)
class RecoveryPolicy:
    failure_threshold: int = 2
    initial_seconds: float = 5
    maximum_seconds: float = 120
    slow_after: int = 6
    cooldown_seconds: float = 600

    def __post_init__(self):
        if (self.failure_threshold < 1 or self.initial_seconds <= 0
                or self.maximum_seconds < self.initial_seconds or self.slow_after < 1
                or self.cooldown_seconds < self.maximum_seconds):
            raise ValueError("invalid recovery policy")


class RecoveryGate:
    def __init__(self, policy=None, *, clock=time.monotonic, jitter=None):
        self.policy = policy or RecoveryPolicy()
        self.clock = clock
        self.jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self.failures = 0
        self.attempts = 0
        self.next_attempt = 0.0
        self.pending = False
        self.reason = None

    def fail(self, reason: str, *, immediate=False):
        self.failures += 1
        self.reason = reason
        if not self.pending and (immediate or self.failures >= self.policy.failure_threshold):
            self.pending = True
            self.next_attempt = max(self.next_attempt, self.clock() + self.policy.initial_seconds)

    @property
    def due(self):
        return self.pending and self.clock() >= self.next_attempt

    def attempted(self):
        """Schedule before the call, so a failed attempt cannot spin on the API."""
        self.attempts += 1
        exponent = min(self.attempts, 20)
        delay = (self.policy.cooldown_seconds if self.attempts >= self.policy.slow_after
                 else min(self.policy.maximum_seconds, self.policy.initial_seconds * 2 ** exponent * self.jitter()))
        self.next_attempt = self.clock() + delay

    def success(self):
        self.failures = self.attempts = 0
        self.pending = False
        self.next_attempt = 0.0
        self.reason = None

    def snapshot(self):
        return {"pending": self.pending, "reason": self.reason, "failures": self.failures,
                "attempts": self.attempts, "retry_in_seconds": max(0, self.next_attempt - self.clock())}
