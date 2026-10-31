"""Regression for GHSA-6v38-4h7w-rj9h: reject an unsigned JSON JWS."""
import pytest
from authlib.jose import JsonWebSignature
from authlib.jose.errors import BadSignatureError


def test_empty_signature_list_is_never_accepted():
    forged = {"payload": "e30", "signatures": []}
    with pytest.raises(BadSignatureError):
        JsonWebSignature().deserialize_json(forged, key=None)
