"""Regression for GHSA-ffc3-869f-jxw9 (PyJWT public-key/HMAC confusion)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def test_mutated_public_pem_cannot_be_used_as_an_hmac_secret():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    public = key.public_bytes(serialization.Encoding.PEM,
                              serialization.PublicFormat.SubjectPublicKeyInfo)
    public = public.replace(b"\n", b"\r")
    serialization.load_pem_public_key(public)  # malformed-looking but valid public PEM

    def encoded(value: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")

    data = encoded({"alg": "HS256", "typ": "JWT"}) + b"." + encoded({"sub": "synthetic"})
    signature = base64.urlsafe_b64encode(hmac.new(public, data, hashlib.sha256).digest()).rstrip(b"=")
    forged = (data + b"." + signature).decode()
    with pytest.raises(jwt.InvalidKeyError):
        jwt.decode(forged, public, algorithms=["HS256", "RS256"])
