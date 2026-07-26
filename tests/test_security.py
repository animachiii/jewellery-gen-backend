from app.core.security import hash_key, verify_key


def test_hash_key_matches_sha256_hexdigest() -> None:
    import hashlib

    assert hash_key("mysecret") == hashlib.sha256(b"mysecret").hexdigest()


def test_verify_key_returns_name_for_known_key() -> None:
    valid_keys = {hash_key("secret123"): "erp"}
    assert verify_key("secret123", valid_keys) == "erp"


def test_verify_key_returns_none_for_unknown_key() -> None:
    valid_keys = {hash_key("secret123"): "erp"}
    assert verify_key("wrong-key", valid_keys) is None


def test_verify_key_returns_none_for_empty_string() -> None:
    valid_keys = {hash_key("secret123"): "erp"}
    assert verify_key("", valid_keys) is None
