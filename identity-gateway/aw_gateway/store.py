"""Small central identity database, separate from every user's Watcher database."""

import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from aw_gateway.security import compare_token, hash_password, token_digest

_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,40}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT,
    legacy_hash TEXT,
    bootstrap_digest TEXT,
    bootstrap_claimed_at INTEGER,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'disabled')),
    slot INTEGER NOT NULL UNIQUE CHECK(slot BETWEEN 1 AND 5),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('bootstrap', 'normal')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS invitations (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    redeemed_by TEXT REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_key_time ON login_attempts(key, occurred_at);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    actor_id TEXT,
    action TEXT NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_callbacks (
    state_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 5),
    provider TEXT NOT NULL CHECK(provider IN ('canvas', 'google')),
    inner_state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_callbacks_expiry ON oauth_callbacks(expires_at);
"""


class IdentityError(ValueError):
    pass


def normalize_username(value: str) -> str:
    value = value.strip()
    if not _USERNAME.fullmatch(value):
        raise IdentityError("Invalid username")
    return value


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or value.count("@") != 1 or any(ch.isspace() for ch in value):
        raise IdentityError("Invalid email address")
    local, domain = value.split("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise IdentityError("Invalid email address")
    return value


class IdentityStore:
    def __init__(self, path: Path, *, slots: int = 5):
        if not 1 <= slots <= 5:
            raise ValueError("This deployment supports 1 to 5 isolated instances")
        self.path = path
        self.slots = slots

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.path.parent, 0o700)
        with self._connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise RuntimeError("Identity database schema is newer than this gateway")
            db.executescript(_SCHEMA)
            db.execute("PRAGMA user_version=2")
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def create_owner(self, username: str, email: str, legacy_hash: str, code: str) -> str:
        username = normalize_username(username)
        email = normalize_email(email)
        if not legacy_hash.startswith("$6$") or len(code) < 32:
            raise IdentityError("Invalid owner bootstrap material")
        user_id = secrets.token_hex(16)
        now = int(time.time())
        with self._write() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise IdentityError("Owner already exists")
            db.execute(
                """INSERT INTO users
                (id, username, email, legacy_hash, bootstrap_digest, must_change_password,
                 role, status, slot, created_at)
                VALUES (?, ?, ?, ?, ?, 1, 'admin', 'active', 1, ?)""",
                (user_id, username, email, legacy_hash, token_digest(code), now),
            )
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'owner_created', ?)",
                (user_id, now),
            )
        return user_id

    def find_user(self, username: str):
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
            ).fetchone()

    def get_user(self, user_id: str):
        with self._connect() as db:
            return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def claim_owner(self, user_id: str, code: str, session_token: str) -> bool:
        now = int(time.time())
        with self._write() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if (
                user is None
                or user["role"] != "admin"
                or user["status"] != "active"
                or not user["must_change_password"]
                or user["bootstrap_claimed_at"] is not None
                or not user["bootstrap_digest"]
                or not compare_token(code, user["bootstrap_digest"])
            ):
                db.execute(
                    "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'owner_claim_failed', ?)",
                    (user_id, now),
                )
                return False
            db.execute(
                "UPDATE users SET bootstrap_digest=NULL, bootstrap_claimed_at=? WHERE id=?",
                (now, user_id),
            )
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, 'bootstrap', ?, ?)",
                (token_digest(session_token), user_id, now, now + 600),
            )
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'owner_claimed', ?)",
                (user_id, now),
            )
            return True

    def finish_owner_password_change(self, session_token: str, password: str, new_token: str):
        password_hash = hash_password(password)
        now = int(time.time())
        with self._write() as db:
            old = db.execute(
                """SELECT s.user_id FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.kind='bootstrap' AND s.expires_at>?
                  AND u.must_change_password=1 AND u.bootstrap_claimed_at IS NOT NULL""",
                (token_digest(session_token), now),
            ).fetchone()
            if old is None:
                raise IdentityError("Bootstrap session is invalid")
            user_id = old["user_id"]
            db.execute(
                """UPDATE users SET password_hash=?, legacy_hash=NULL, bootstrap_digest=NULL,
                   must_change_password=0 WHERE id=?""",
                (password_hash, user_id),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, 'normal', ?, ?)",
                (token_digest(new_token), user_id, now, now + 7 * 86400),
            )
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'owner_password_changed', ?)",
                (user_id, now),
            )
            return user_id

    def create_session(self, user_id: str, token: str) -> None:
        now = int(time.time())
        with self._write() as db:
            user = db.execute("SELECT status, must_change_password FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None or user["status"] != "active" or user["must_change_password"]:
                raise IdentityError("Account unavailable")
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, 'normal', ?, ?)",
                (token_digest(token), user_id, now, now + 7 * 86400),
            )

    def session(self, token: str):
        if not token:
            return None
        now = int(time.time())
        with self._connect() as db:
            return db.execute(
                """SELECT u.*, s.kind AS session_kind FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND u.status='active'""",
                (token_digest(token), now),
            ).fetchone()

    def revoke_session(self, token: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(token),))

    def record_login_attempt(self, key: str, *, limit: int = 10, window: int = 900) -> bool:
        """Count before attempting an expensive password hash; don't store raw usernames/IPs."""
        now = int(time.time())
        key_hash = token_digest(key)
        with self._write() as db:
            db.execute("DELETE FROM login_attempts WHERE occurred_at<?", (now - window,))
            attempts = db.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE key=? AND occurred_at>=?",
                (key_hash, now - window),
            ).fetchone()[0]
            if attempts >= limit:
                return False
            db.execute("INSERT INTO login_attempts VALUES (?, ?)", (key_hash, now))
            return True

    def clear_login_attempts(self, key: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM login_attempts WHERE key=?", (token_digest(key),))

    def create_invitation(self, actor_id: str, email: str, token: str) -> None:
        email = normalize_email(email)
        now = int(time.time())
        with self._write() as db:
            actor = db.execute("SELECT role, status, must_change_password FROM users WHERE id=?", (actor_id,)).fetchone()
            if actor is None or actor["role"] != "admin" or actor["status"] != "active" or actor["must_change_password"]:
                raise IdentityError("Administrator required")
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= self.slots:
                raise IdentityError("No isolated instance slots remain")
            db.execute(
                "INSERT INTO invitations VALUES (?, ?, ?, ?, ?, NULL)",
                (token_digest(token), email, actor_id, now, now + 7 * 86400),
            )
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'invitation_created', ?)",
                (actor_id, now),
            )

    def invitation(self, token: str):
        now = int(time.time())
        with self._connect() as db:
            return db.execute(
                "SELECT email FROM invitations WHERE token_hash=? AND expires_at>? AND redeemed_by IS NULL",
                (token_digest(token), now),
            ).fetchone()

    def redeem_invitation(self, token: str, username: str, email: str, password: str):
        username = normalize_username(username)
        email = normalize_email(email)
        password_hash = hash_password(password)
        now = int(time.time())
        with self._write() as db:
            invite = db.execute(
                "SELECT email FROM invitations WHERE token_hash=? AND expires_at>? AND redeemed_by IS NULL",
                (token_digest(token), now),
            ).fetchone()
            if invite is None or invite["email"] != email:
                raise IdentityError("Invitation invalid or expired")
            slots_used = {row[0] for row in db.execute("SELECT slot FROM users")}
            slot = next((number for number in range(2, self.slots + 1) if number not in slots_used), None)
            if slot is None:
                raise IdentityError("No isolated instance slots remain")
            user_id = secrets.token_hex(16)
            try:
                db.execute(
                    """INSERT INTO users
                    (id, username, email, password_hash, must_change_password, role, status, slot, created_at)
                    VALUES (?, ?, ?, ?, 0, 'user', 'pending', ?, ?)""",
                    (user_id, username, email, password_hash, slot, now),
                )
            except sqlite3.IntegrityError as error:
                raise IdentityError("Username or email unavailable") from error
            db.execute(
                "UPDATE invitations SET redeemed_by=? WHERE token_hash=?",
                (user_id, token_digest(token)),
            )
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'invitation_redeemed', ?)",
                (user_id, now),
            )
            return user_id, slot

    def activate_user(self, user_id: str) -> None:
        with self._write() as db:
            result = db.execute(
                "UPDATE users SET status='active' WHERE id=? AND status='pending'", (user_id,)
            )
            if result.rowcount != 1:
                raise IdentityError("Pending user not found")
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'user_activated', ?)",
                (user_id, int(time.time())),
            )

    def disable_user(self, actor_id: str, target_id: str) -> None:
        if actor_id == target_id:
            raise IdentityError("Administrator cannot disable own account")
        with self._write() as db:
            actor = db.execute("SELECT role, status, must_change_password FROM users WHERE id=?", (actor_id,)).fetchone()
            if actor is None or actor["role"] != "admin" or actor["status"] != "active" or actor["must_change_password"]:
                raise IdentityError("Administrator required")
            result = db.execute(
                "UPDATE users SET status='disabled' WHERE id=? AND status IN ('pending', 'active')",
                (target_id,),
            )
            if result.rowcount != 1:
                raise IdentityError("User not found or already disabled")
            db.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'user_disabled', ?)",
                (actor_id, int(time.time())),
            )

    def list_users(self):
        with self._connect() as db:
            return db.execute(
                "SELECT id, username, email, role, status, slot, created_at FROM users ORDER BY slot"
            ).fetchall()

    def register_oauth_callback(
        self, *, outer_state: str, user_id: str, slot: int, provider: str,
        inner_state: str, lifetime_seconds: int,
    ) -> None:
        if provider not in {"canvas", "google"} or not inner_state or not 0 < lifetime_seconds <= 600:
            raise IdentityError("Invalid OAuth callback mapping")
        now = int(time.time())
        with self._write() as db:
            user = db.execute("SELECT status, slot FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None or user["status"] != "active" or user["slot"] != slot:
                raise IdentityError("OAuth user is unavailable")
            db.execute("DELETE FROM oauth_callbacks WHERE expires_at<=?", (now,))
            db.execute(
                "INSERT INTO oauth_callbacks VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_digest(outer_state), user_id, slot, provider, inner_state, now, now + lifetime_seconds),
            )

    def consume_oauth_callback(self, outer_state: str, provider: str):
        now = int(time.time())
        with self._write() as db:
            mapping = db.execute(
                """SELECT c.slot, c.inner_state FROM oauth_callbacks c
                JOIN users u ON u.id=c.user_id
                WHERE c.state_hash=? AND c.provider=? AND c.expires_at>?
                  AND u.status='active' AND u.slot=c.slot""",
                (token_digest(outer_state), provider, now),
            ).fetchone()
            if mapping is None:
                return None
            db.execute("DELETE FROM oauth_callbacks WHERE state_hash=?", (token_digest(outer_state),))
            return mapping["slot"], mapping["inner_state"]
