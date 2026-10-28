"""Fixture generated independently with `openssl passwd -6 -salt awfixture`."""

from aw_gateway.security import verify_legacy_password

_OPENSSL_SHA512_CRYPT = (
    "$6$awfixture$hwuRg1k3n0txaihHtr0ZuVtbkTDZ6uqB8dqZc7MrD3Ggb8KfiXQ5xjuBdgSuwvPz"
    "Kl1yGCvB4Ffte03X.DcQg."
)


def test_legacy_sha512_crypt_fixture():
    assert verify_legacy_password("fixture password 123", _OPENSSL_SHA512_CRYPT)
    assert not verify_legacy_password("wrong password", _OPENSSL_SHA512_CRYPT)
    assert not verify_legacy_password("fixture password 123", "$5$awfixture$invalid")
