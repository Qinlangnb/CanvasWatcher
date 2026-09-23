"""Process-local, expiring capabilities. No provider secrets are stored here."""

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class ChallengeError(ValueError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(repr=False)
class Challenge:
    id: str
    provider: str
    target: dict
    deadline: float
    ticket_hash: str
    control_hash: str
    exchange_hash: str = ""
    state: str = "WAITING_FOR_USER"
    claimed: bool = False
    finishing: bool = False
    reason: str | None = None
    created: float = field(default_factory=time.monotonic)


class ChallengeStore:
    def __init__(self, ttl: int = 240, clock: Callable = time.monotonic):
        self.instance_id = secrets.token_urlsafe(24)
        self.ttl = ttl
        self.clock = clock
        self.lock = threading.RLock()
        self.rows: dict[str, Challenge] = {}

    def create(self, provider: str, target: dict) -> dict:
        with self.lock:
            self.rows = {k: v for k, v in self.rows.items() if v.deadline + 300 > self.clock()}
            if len(self.rows) >= 64:
                raise ChallengeError("AUTH_BROKER_BUSY")
            # A newer attempt supersedes the same profile's pending attempt.
            for row in self.rows.values():
                if row.provider == provider and row.target.get("credential_id") == target.get("credential_id"):
                    if row.state in {"WAITING_FOR_USER", "LOGIN_OPENED"}:
                        row.state = "CANCELLED"
            ticket, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            row = Challenge(secrets.token_urlsafe(24), provider, dict(target), self.clock() + self.ttl,
                            digest(ticket), digest(control))
            self.rows[row.id] = row
            return {"challenge_id": row.id, "instance_id": self.instance_id, "provider": provider,
                    "one_time_token": ticket, "control_token": control, "expires_in": self.ttl,
                    "protocol_version": 1}

    def _row(self, challenge_id: str, instance_id: str, provider: str) -> Challenge:
        row = self.rows.get(challenge_id)
        if instance_id != self.instance_id or row is None or row.provider != provider:
            raise ChallengeError("AUTH_CHALLENGE_INVALID")
        if row.deadline <= self.clock() and row.state in {"WAITING_FOR_USER", "LOGIN_OPENED"}:
            row.state, row.reason = "TIMED_OUT", "AUTH_CHALLENGE_EXPIRED"
        return row

    def _live(self, row: Challenge):
        if row.state == "TIMED_OUT":
            raise ChallengeError("AUTH_CHALLENGE_EXPIRED")
        if row.state not in {"WAITING_FOR_USER", "LOGIN_OPENED"}:
            raise ChallengeError("AUTH_CHALLENGE_INVALID")

    def claim(self, challenge_id: str, instance_id: str, provider: str, token: str) -> dict:
        with self.lock:
            row = self._row(challenge_id, instance_id, provider)
            self._live(row)
            if row.claimed or not secrets.compare_digest(row.ticket_hash, digest(token)):
                raise ChallengeError("AUTH_CHALLENGE_INVALID")
            exchange = secrets.token_urlsafe(32)
            row.claimed, row.exchange_hash, row.state = True, digest(exchange), "LOGIN_OPENED"
            return {**row.target, "provider": row.provider, "exchange_token": exchange,
                    "expires_in": max(0, int(row.deadline - self.clock())), "protocol_version": 1}

    def begin_completion(self, challenge_id: str, instance_id: str, provider: str, token: str) -> Challenge:
        with self.lock:
            row = self._row(challenge_id, instance_id, provider)
            self._live(row)
            if not row.claimed or row.finishing or not secrets.compare_digest(row.exchange_hash, digest(token)):
                raise ChallengeError("AUTH_CHALLENGE_INVALID")
            row.finishing = True
            return row

    def finish(self, row: Challenge, publish: Callable[[], None]) -> None:
        with self.lock:
            self._row(row.id, self.instance_id, row.provider)
            self._live(row)
            publish()  # synchronous publication; cancel cannot interleave after this check
            row.state, row.exchange_hash = "AUTHENTICATED", ""

    def fail(self, row: Challenge, reason: str = "AUTH_LOGIN_FAILED") -> None:
        with self.lock:
            if row.state in {"LOGIN_OPENED", "WAITING_FOR_USER"}:
                row.state, row.reason, row.exchange_hash = "FAILED", reason, ""

    def control(self, challenge_id: str, instance_id: str, provider: str, token: str, cancel=False) -> dict:
        with self.lock:
            row = self._row(challenge_id, instance_id, provider)
            if not secrets.compare_digest(row.control_hash, digest(token)):
                raise ChallengeError("AUTH_CHALLENGE_INVALID")
            if cancel and row.state in {"WAITING_FOR_USER", "LOGIN_OPENED"}:
                row.state, row.reason = "CANCELLED", "AUTH_USER_CANCELLED"
            return {"state": row.state, "reason": row.reason, "provider": row.provider}


broker_challenges = ChallengeStore()
