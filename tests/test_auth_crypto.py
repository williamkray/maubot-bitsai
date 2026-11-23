import base64
import os

import pytest

from bitsai.auth.crypto import SecretBox


def key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_roundtrip_and_format():
    box = SecretBox(key())
    blob = box.encrypt("hunter2-token", "@a:x\x00access_token_enc")
    raw = base64.b64decode(blob)
    assert raw[0] == 1 and len(raw) == 1 + 12 + 16 + len("hunter2-token")
    assert "hunter2" not in blob
    assert box.decrypt(blob, "@a:x\x00access_token_enc") == "hunter2-token"


def test_nonce_random():
    box = SecretBox(key())
    assert box.encrypt("t", "a") != box.encrypt("t", "a")


def test_aad_swap_rejected():
    box = SecretBox(key())
    blob = box.encrypt("t", "@alice:x\x00access_token_enc")
    for bad in ("@bob:x\x00access_token_enc", "@alice:x\x00refresh_token_enc"):
        with pytest.raises(ValueError):
            box.decrypt(blob, bad)


def test_tamper_rejected():
    box = SecretBox(key())
    raw = bytearray(base64.b64decode(box.encrypt("secret", "a")))
    for idx in (0, 5, 14, len(raw) - 1):
        mod = bytearray(raw)
        mod[idx] ^= 1
        with pytest.raises(ValueError):
            box.decrypt(base64.b64encode(bytes(mod)).decode(), "a")
    for junk in ("", "!!!", base64.b64encode(b"short").decode()):
        with pytest.raises(ValueError):
            box.decrypt(junk, "a")


def test_wrong_key_rejected():
    blob = SecretBox(key()).encrypt("t", "a")
    with pytest.raises(ValueError):
        SecretBox(key()).decrypt(blob, "a")


@pytest.mark.parametrize("n", [16, 31, 33, 64])
def test_bad_key_length_message_has_no_key(n):
    k = base64.b64encode(os.urandom(n)).decode()
    with pytest.raises(ValueError) as ei:
        SecretBox(k)
    assert k not in str(ei.value) and k not in repr(ei.value)


def test_bad_key_not_base64():
    with pytest.raises(ValueError) as ei:
        SecretBox("not base64 !!! secret-key-material")
    assert "secret-key-material" not in str(ei.value)


def test_repr_hides_key():
    k = key()
    assert k not in repr(SecretBox(k))
