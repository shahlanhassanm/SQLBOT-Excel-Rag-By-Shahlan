"""Coverage for two modules the audit found completely untested
(AUDIT C-03 `apps/db/db.py`, C-04 `apps/system/middleware/auth.py`).

C-04 settles **Q-18**: the audit asked whether the two-step decode that starts
with `verify_signature: False` is deliberate. It is, on BOTH paths — you cannot
check a signature before you know which key signed it, and each path re-decodes
with the right secret and full verification. The source's own comment claiming
embedded tokens were unverified was stale, and is corrected.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_auth_and_db_coverage.py -q"
"""

import datetime
import decimal
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _backend():
    for c in (REPO_ROOT / "backend", pathlib.Path("/opt/sqlbot/app")):
        if (c / "main.py").exists():
            return c
    pytest.skip("backend tree not found")


def _auth_src():
    return (_backend() / "apps/system/middleware/auth.py").read_text(encoding="utf-8")


# =====================================================================
# C-04 / Q-18 — the two JWT decode paths are NOT equivalent
# =====================================================================

def test_api_key_path_reverifies_with_the_key_secret():
    """`validateAskToken` decodes UNVERIFIED first only to read `access_key` —
    you cannot verify a signature before you know which key signed it — then
    re-decodes with that key's `secret_key` and full verification.

    That is the correct, standard pattern, and it answers Q-18: the unverified
    first pass is deliberate. This test exists so nobody "hardens" it by
    deleting the first decode, which would break API-key auth entirely."""
    src = _auth_src()
    m = re.search(r"async def validateAskToken\(.*?(?=\n    async def |\n    def )",
                  src, re.S)
    assert m, "validateAskToken not found"
    body = m.group(0)
    assert body.count("jwt.decode") == 2, (
        "the API-key path must decode twice: once to find the key, once to verify")
    assert "api_key_model.secret_key" in body, (
        "the second decode must verify against the key's secret")
    verified = body.split("verify_signature", 1)[1]
    assert "secret_key" in verified, "no verified decode follows the unverified one"


def test_embedded_path_also_reverifies_against_the_assistant_secret():
    """The embedded path uses the SAME two-step pattern: unverified decode to
    learn which assistant, then a verified decode against that assistant's
    `app_secret`.

    The source used to carry a comment saying "Signature verification is
    disabled for embedded tokens / This is a security risk". That comment was
    STALE — the verified second decode is right there. A false security warning
    is worse than none: it invites a "fix" that would break embedded auth. This
    test pins the real behaviour so the claim cannot drift again (AUDIT Q-18)."""
    src = _auth_src()
    m = re.search(r"async def validateEmbedded\(.*?(?=\n    async def |\n    def |\ndef )",
                  src, re.S)
    assert m, "validateEmbedded not found"
    body = m.group(0)
    assert body.count("jwt.decode") == 2, (
        "the embedded path must decode twice: once to find the assistant, "
        "once to verify")
    assert "app_secret" in body, (
        "the second decode must verify against the assistant's app_secret")
    verified = body.split("verify_signature", 1)[1]
    assert "app_secret" in verified, "no verified decode follows the unverified one"


def test_no_stale_security_warning_remains():
    """Guards the correction itself."""
    assert "Signature verification is disabled" not in _auth_src(), (
        "the stale WARNING comment is back; it misdescribes the code (Q-18)")


def test_ask_token_requires_the_sk_scheme():
    src = _auth_src()
    assert 'schema.lower() != "sk"' in src, (
        "the ask-token scheme check was removed; any Authorization scheme would pass")


def test_xor_decrypt_roundtrips():
    """`xor_decrypt` derives the embedded assistant id when the token omits it,
    so a wrong result silently selects the WRONG assistant."""
    import main  # noqa: F401  (production import order; see D-39)
    from apps.system.middleware.auth import xor_decrypt

    import base64

    key = 0xABCD1234
    for value in (1, 7, 42, 65535, 2**31 - 1):
        num = value ^ key
        raw = num.to_bytes((num.bit_length() + 7) // 8 or 1, "big")
        encrypted = base64.urlsafe_b64encode(raw).decode()
        assert xor_decrypt(encrypted, key) == value, value


def test_xor_decrypt_raises_on_garbage_and_the_caller_catches_it():
    """`xor_decrypt` base64-decodes attacker-controlled token content, so it
    DOES raise on malformed input. That is safe only because its sole call site
    sits inside `validateEmbedded`'s try/except, which turns the failure into an
    auth rejection rather than a 500.

    Both halves are asserted, because the safety is a property of the pair: move
    the call out of the try block and this becomes a crash on hostile input."""
    import main  # noqa: F401
    from apps.system.middleware.auth import xor_decrypt

    raised = 0
    for junk in ("!!", "0x", "zz z"):
        try:
            xor_decrypt(junk)
        except Exception:
            raised += 1
    assert raised, "expected malformed base64 to raise"

    src = _auth_src()
    m = re.search(r"async def validateEmbedded\(.*?(?=\n    async def |\n    def |\ndef )",
                  src, re.S)
    body = m.group(0)
    assert "xor_decrypt(" in body
    assert "try:" in body.split("xor_decrypt(")[0], (
        "xor_decrypt is no longer inside validateEmbedded's try block; malformed "
        "token content would now surface as a 500 instead of an auth failure")
    assert "except" in body.split("xor_decrypt(")[1]


# =====================================================================
# C-03 — db.convert_value runs per CELL of every result row
# =====================================================================

def _convert():
    import main  # noqa: F401
    from apps.db.db import convert_value
    return convert_value


@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("plain", "plain"),
    (1, 1),
    (True, True),
])
def test_convert_value_passes_simple_types_through(value, expected):
    assert _convert()(value) == expected


def test_convert_value_serialises_datetime_with_a_space():
    out = _convert()(datetime.datetime(2024, 1, 15, 14, 30, 45), "space")
    assert out == "2024-01-15 14:30:45", out


def test_convert_value_serialises_datetime_iso():
    out = _convert()(datetime.datetime(2024, 1, 15, 14, 30, 45), "iso")
    assert "T" in str(out), out


def test_convert_value_makes_decimal_json_safe():
    """Decimal is not JSON-serialisable; leaking one raises at response time,
    long after this function ran."""
    import json

    out = _convert()(decimal.Decimal("1.25"))
    json.dumps(out)


def test_convert_value_makes_bytes_json_safe():
    import json

    json.dumps(_convert()(b"\x00\x01\xff"))


def test_convert_value_never_raises_on_an_unknown_type():
    """It runs per cell of every result row, so one exotic driver type must not
    take down the whole answer."""
    class _Exotic:
        def __str__(self):
            return "exotic"

    _convert()(_Exotic())
