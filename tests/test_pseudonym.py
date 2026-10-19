"""Pseudonym algorithm tests: format, determinism, namespace, validation."""

import pytest

from app.pseudonym import PseudonymError, compute_pseudonym, is_valid_pseudonym, key_fingerprint

KEY = "k" * 48  # raw 48 bytes >= 32


def test_format():
    p = compute_pseudonym(3, KEY)
    assert p.startswith("P-")
    assert len(p) == 12  # "P-" + 10 chars
    assert is_valid_pseudonym(p)


def test_determinism_same_call():
    assert compute_pseudonym(3, KEY) == compute_pseudonym(3, KEY)


def test_different_ids_differ():
    assert compute_pseudonym(3, KEY) != compute_pseudonym(4, KEY)


def test_key_changes_output():
    assert compute_pseudonym(3, KEY) != compute_pseudonym(3, "j" * 48)


def test_namespaced_input():
    # namespacing: "canvas-user:3" != "3" — verify input string construction
    import base64, hmac, hashlib

    expected = "P-" + base64.b32encode(
        hmac.new(KEY.encode(), b"canvas-user:3", hashlib.sha256).digest()
    ).decode()[:10]
    assert compute_pseudonym(3, KEY) == expected


def test_base64_prefixed_key():
    import base64

    k = base64.b64encode(b"y" * 48).decode()
    assert compute_pseudonym(7, f"b64:{k}") == compute_pseudonym(7, "y" * 48)


def test_base64_short_decoded_key_rejected():
    import base64

    with pytest.raises(PseudonymError):
        compute_pseudonym(3, "b64:" + base64.b64encode(b"z" * 16).decode())


def test_invalid_inputs_fail_closed():
    for bad in ("3", 3.5, -1, 0, True, None):
        with pytest.raises(PseudonymError):
            compute_pseudonym(bad, KEY)  # type: ignore[arg-type]


def test_missing_key_fails():
    with pytest.raises(PseudonymError):
        compute_pseudonym(3, "")


def test_short_key_rejected():
    with pytest.raises(PseudonymError):
        compute_pseudonym(3, "short")


def test_validation_rejects_malformed():
    for bad in ("P-abc", "p-ABCDEFGHIJ", "P-ABCDEFGHIJ1", "P-ABCDEF1HIJ", "X-ABCDEFGHIJ", "", "P-ABCDEFGHI"):
        assert not is_valid_pseudonym(bad)


def test_key_fingerprint():
    fp = key_fingerprint(KEY)
    assert len(fp) == 8 and int(fp, 16) >= 0
