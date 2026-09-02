from src.web.passwords import hash_password, verify_password


def test_hash_and_verify_roundtrip() -> None:
    h = hash_password("S3cret!")
    assert h != "S3cret!"
    assert verify_password("S3cret!", h) is True
    assert verify_password("wrong", h) is False


def test_hashes_are_salted_unique() -> None:
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_garbage() -> None:
    assert verify_password("x", "not-a-valid-hash") is False
