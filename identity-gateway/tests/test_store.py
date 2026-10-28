import pytest
from passlib.hash import sha512_crypt

from aw_gateway.security import issue_token, verify_legacy_password, verify_password
from aw_gateway.store import IdentityError, IdentityStore


def test_owner_claim_is_single_use_and_forces_new_password(tmp_path):
    store = IdentityStore(tmp_path / "identity.db")
    store.initialize()
    legacy = sha512_crypt.hash("temporary old password")
    assert verify_legacy_password("temporary old password", legacy)
    assert not verify_legacy_password("incorrect", legacy)
    code = issue_token()
    owner = store.create_owner("example_admin", "admin@example.com", legacy, code)
    bootstrap_session = issue_token()
    assert not store.claim_owner(owner, "wrong", bootstrap_session)
    assert store.claim_owner(owner, code, bootstrap_session)
    assert not store.claim_owner(owner, code, issue_token())
    assert store.session(bootstrap_session)["session_kind"] == "bootstrap"
    normal_session = issue_token()
    assert store.finish_owner_password_change(bootstrap_session, "long new password", normal_session) == owner
    assert store.session(bootstrap_session) is None
    assert store.session(normal_session)["session_kind"] == "normal"
    user = store.get_user(owner)
    assert user["legacy_hash"] is None
    assert user["bootstrap_digest"] is None
    assert verify_password("long new password", user["password_hash"])
    assert not verify_password("temporary old password", user["password_hash"])


def test_invitation_slot_and_disable_isolate_accounts(tmp_path):
    store = IdentityStore(tmp_path / "identity.db", slots=2)
    store.initialize()
    code = issue_token()
    owner = store.create_owner(
        "example_admin", "admin@example.com", sha512_crypt.hash("legacy password"), code
    )
    bootstrap = issue_token()
    assert store.claim_owner(owner, code, bootstrap)
    store.finish_owner_password_change(bootstrap, "owner new password", issue_token())
    invite = issue_token()
    store.create_invitation(owner, "student@example.com", invite)
    assert store.invitation(invite)["email"] == "student@example.com"
    with pytest.raises(IdentityError):
        store.redeem_invitation(invite, "student", "wrong@example.com", "student password")
    user_id, slot = store.redeem_invitation(
        invite, "student", "student@example.com", "student password"
    )
    assert slot == 2
    assert store.invitation(invite) is None
    assert store.get_user(user_id)["status"] == "pending"
    with pytest.raises(IdentityError):
        store.create_invitation(owner, "another@example.com", issue_token())
    store.activate_user(user_id)
    session = issue_token()
    store.create_session(user_id, session)
    assert store.session(session)["id"] == user_id
    store.disable_user(owner, user_id)
    assert store.session(session) is None
    assert store.get_user(user_id)["status"] == "disabled"


def test_login_attempt_limit_and_owner_uniqueness(tmp_path):
    store = IdentityStore(tmp_path / "identity.db")
    store.initialize()
    owner = store.create_owner(
        "example_admin", "admin@example.com", sha512_crypt.hash("legacy password"), issue_token()
    )
    assert store.find_user("example_admin")["id"] == owner
    with pytest.raises(IdentityError):
        store.create_owner("another", "another@example.com", sha512_crypt.hash("pw"), issue_token())
    assert store.record_login_attempt("ip:name", limit=2)
    assert store.record_login_attempt("ip:name", limit=2)
    assert not store.record_login_attempt("ip:name", limit=2)
    store.clear_login_attempts("ip:name")
    assert store.record_login_attempt("ip:name", limit=2)
