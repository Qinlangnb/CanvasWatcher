"""Process-local outer capabilities bind desktop Broker callbacks to one tenant."""

import threading
import time
from dataclasses import dataclass

from aw_gateway.security import compare_token, issue_token, token_digest


class BrokerMappingError(ValueError):
    pass


@dataclass(repr=False)
class BrokerMapping:
    outer_id: str
    inner_id: str
    instance_id: str
    provider: str
    user_id: str
    slot: int
    ticket_hash: str
    inner_ticket: str
    control_hash: str
    inner_control: str
    deadline: float
    exchange_hash: str = ""
    inner_exchange: str = ""

    def matches(self, payload: dict) -> bool:
        return (
            payload.get("challenge_id") == self.outer_id
            and payload.get("instance_id") == self.instance_id
            and payload.get("provider") == self.provider
        )

    def inner_identity(self) -> dict:
        return {
            "challenge_id": self.inner_id,
            "instance_id": self.instance_id,
            "provider": self.provider,
        }


class BrokerMap:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.rows: dict[str, BrokerMapping] = {}

    def _lookup(self, payload: dict) -> BrokerMapping:
        row = self.rows.get(str(payload.get("challenge_id", "")))
        if row is None or row.deadline <= self.clock() or not row.matches(payload):
            raise BrokerMappingError("AUTH_CHALLENGE_INVALID")
        return row

    def create(self, backend_ticket: dict, *, user_id: str, slot: int) -> dict:
        try:
            inner_id = backend_ticket["challenge_id"]
            instance_id = backend_ticket["instance_id"]
            provider = backend_ticket["provider"]
            inner_ticket = backend_ticket["one_time_token"]
            inner_control = backend_ticket["control_token"]
            lifetime = int(backend_ticket["expires_in"])
        except (KeyError, TypeError, ValueError) as error:
            raise BrokerMappingError("AUTH_CHALLENGE_INVALID") from error
        if not all(isinstance(value, str) and 16 <= len(value) <= 256 for value in (
            inner_id, instance_id, inner_ticket, inner_control,
        )) or provider not in {"canvas", "gradescope", "prairielearn", "all"} or not 0 < lifetime <= 240:
            raise BrokerMappingError("AUTH_CHALLENGE_INVALID")
        outer_id, outer_ticket, outer_control = issue_token(), issue_token(), issue_token()
        with self.lock:
            self.rows = {key: row for key, row in self.rows.items() if row.deadline > self.clock()}
            if len(self.rows) >= 64:
                raise BrokerMappingError("AUTH_BROKER_BUSY")
            self.rows[outer_id] = BrokerMapping(
                outer_id, inner_id, instance_id, provider, user_id, slot,
                token_digest(outer_ticket), inner_ticket, token_digest(outer_control),
                inner_control, self.clock() + lifetime,
            )
        return {
            "challenge_id": outer_id, "instance_id": instance_id, "provider": provider,
            "one_time_token": outer_ticket, "control_token": outer_control,
            "expires_in": lifetime, "protocol_version": backend_ticket.get("protocol_version", 1),
        }

    def browser_control(self, payload: dict, *, user_id: str) -> tuple[int, dict]:
        with self.lock:
            row = self._lookup(payload)
            token = payload.get("control_token")
            if row.user_id != user_id or not isinstance(token, str) or not compare_token(token, row.control_hash):
                raise BrokerMappingError("AUTH_CHALLENGE_INVALID")
            return row.slot, {**row.inner_identity(), "control_token": row.inner_control}

    def broker_claim(self, payload: dict) -> tuple[int, dict]:
        with self.lock:
            row = self._lookup(payload)
            token = payload.get("one_time_token")
            if not isinstance(token, str) or not compare_token(token, row.ticket_hash):
                raise BrokerMappingError("AUTH_CHALLENGE_INVALID")
            return row.slot, {**row.inner_identity(), "one_time_token": row.inner_ticket}

    def exchange(self, payload: dict, inner_exchange: str) -> str:
        if not isinstance(inner_exchange, str) or len(inner_exchange) < 16:
            raise BrokerMappingError("AUTH_CHALLENGE_INVALID")
        with self.lock:
            row = self._lookup(payload)
            if row.exchange_hash:
                raise BrokerMappingError("AUTH_CHALLENGE_INVALID")
            outer_exchange = issue_token()
            row.inner_exchange = inner_exchange
            row.exchange_hash = token_digest(outer_exchange)
            return outer_exchange

    def broker_completion(self, payload: dict) -> tuple[int, dict]:
        with self.lock:
            row = self._lookup(payload)
            token = payload.get("exchange_token")
            if not row.exchange_hash or not isinstance(token, str) or not compare_token(token, row.exchange_hash):
                raise BrokerMappingError("AUTH_CHALLENGE_INVALID")
            return row.slot, {
                **row.inner_identity(),
                **{key: value for key, value in payload.items() if key not in {
                    "challenge_id", "instance_id", "provider", "exchange_token",
                }},
                "exchange_token": row.inner_exchange,
            }

    def finish(self, payload: dict) -> None:
        with self.lock:
            row = self._lookup(payload)
            self.rows.pop(row.outer_id, None)
