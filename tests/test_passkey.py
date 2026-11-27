"""Přihlášení Windows Hello (WebAuthn): simulovaný autentizátor, jen syntetické klíče."""

from __future__ import annotations

import hashlib
import json
import stat
import struct
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from test_webui import authenticated_handler, call_handler

from agent_lease_mcp import passkey
from agent_lease_mcp.config import Settings
from agent_lease_mcp.passkey import PasskeyError, Passkeys, b64u, cbor_decode
from agent_lease_mcp.store import Store
from agent_lease_mcp.webui import SESSION_COOKIE, make_handler

ORIGIN = "http://localhost:8765"


def cbor(v) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
        raise ValueError
    if isinstance(v, bool):
        return bytes([0xF5 if v else 0xF4])
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())


class Authenticator:
    """Chová se jako Windows Hello: platformní klíč, ověření uživatele PINem."""

    def __init__(self, alg=-7):
        self.alg, self.count, self.cred_id = alg, 0, b"\x11" * 32
        if alg == -7:
            self.key = ec.generate_private_key(ec.SECP256R1())
            nums = self.key.public_key().public_numbers()
            self.cose = {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")}
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            nums = self.key.public_key().public_numbers()
            self.cose = {1: 3, 3: -257, -1: nums.n.to_bytes(256, "big"), -2: nums.e.to_bytes(3, "big")}

    def client(self, kind, challenge, origin=ORIGIN):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def auth(self, flags=0x05, rp="localhost", extra=b""):
        self.count += 1
        return hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + extra

    def create(self, challenge, **kw):
        cred = b"\x00" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + cbor(self.cose)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": self.auth(flags=0x45, extra=cred)})
        return {"id": b64u(self.cred_id), "clientDataJSON": b64u(self.client("webauthn.create", challenge, **kw)),
                "attestationObject": b64u(att)}

    def get(self, challenge, flags=0x05, origin=ORIGIN, rp="localhost", tamper=False):
        client = self.client("webauthn.get", challenge, origin)
        auth = self.auth(flags=flags, rp=rp)
        signed = auth + hashlib.sha256(client).digest()
        sig = (self.key.sign(signed, ec.ECDSA(hashes.SHA256())) if self.alg == -7
               else self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256()))
        if tamper:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        return {"id": b64u(self.cred_id), "clientDataJSON": b64u(client),
                "authenticatorData": b64u(auth), "signature": b64u(sig)}


def keys_for(tmp_path):
    return Passkeys(tmp_path, {ORIGIN, "http://127.0.0.1:8765"})


@pytest.mark.parametrize("alg", [-7, -257])
def test_register_and_login_with_pin(tmp_path: Path, alg):
    pk, dev = keys_for(tmp_path), Authenticator(alg)
    pk.register(dev.create(pk.register_options("michal")["challenge"]))
    assert stat.S_IMODE((tmp_path / passkey.PASSKEY_FILE).stat().st_mode) == 0o600
    assert pk.login(dev.get(pk.login_options()["challenge"]))["id"] == b64u(dev.cred_id)
    assert pk.public_list()[0]["last_used"]


@pytest.mark.parametrize("attack,error", [
    ({"origin": "http://evil.example"}, "wrong origin"),
    ({"origin": "http://127.0.0.1:8765"}, "wrong origin"),          # RP ID je localhost, IP nepřipouštíme
    ({"flags": 0x01}, "user verification"),                           # bez PINu
    ({"rp": "evil.example"}, "wrong relying party"),
    ({"tamper": True}, "bad signature"),
])
def test_login_rejects_attacks(tmp_path: Path, attack, error):
    pk, dev = keys_for(tmp_path), Authenticator()
    pk.register(dev.create(pk.register_options("michal")["challenge"]))
    with pytest.raises(PasskeyError, match=error):
        pk.login(dev.get(pk.login_options()["challenge"], **attack))


def test_challenge_is_single_use_and_bound_to_purpose(tmp_path: Path):
    pk, dev = keys_for(tmp_path), Authenticator()
    reg = pk.register_options("michal")["challenge"]
    pk.register(dev.create(reg))
    with pytest.raises(PasskeyError, match="challenge"):
        pk.register(dev.create(reg))                                     # opakovaná výzva
    with pytest.raises(PasskeyError, match="challenge"):
        pk.login(dev.get(pk.register_options("michal")["challenge"]))    # výzva pro registraci nejde na login
    with pytest.raises(PasskeyError, match="challenge"):
        pk.login(dev.get("made-up"))


def test_counter_must_increase(tmp_path: Path):
    pk, dev = keys_for(tmp_path), Authenticator()
    pk.register(dev.create(pk.register_options("michal")["challenge"]))
    pk.login(dev.get(pk.login_options()["challenge"]))
    dev.count = 0                                                         # naklonovaný klíč se starým čítačem
    with pytest.raises(PasskeyError, match="counter"):
        pk.login(dev.get(pk.login_options()["challenge"]))


def test_unknown_key_and_no_keys(tmp_path: Path):
    pk = keys_for(tmp_path)
    assert pk.login_options()["allowCredentials"] == [] and pk.login_options()["challenge"] == ""
    pk.register(Authenticator().create(pk.register_options("michal")["challenge"]))
    with pytest.raises(PasskeyError, match="unknown key"):
        pk.login({**Authenticator().get(pk.login_options()["challenge"]), "id": b64u(b"\x22" * 32)})


def test_cbor_decoder_rejects_malformed_input():
    assert cbor_decode(cbor({"a": [1, -2, b"x"]})) == {"a": [1, -2, b"x"]}
    for bad in (b"", b"\x5f", b"\x9f", b"\x42\x00", cbor([1]) + b"\x00", b"\x81" * 20 + b"\x00"):
        with pytest.raises(PasskeyError):
            cbor_decode(bad)


def handler_env(tmp_path):
    settings = Settings(db_path=tmp_path / "room.db", agent="test-web", default_ttl=1800)
    return settings, Store(settings=settings)


def test_full_http_flow_issues_session_and_audits(tmp_path: Path):
    settings, store = handler_env(tmp_path)
    h = authenticated_handler(store, settings, "secret")
    dev = Authenticator()
    status, raw = call_handler(store, settings, "secret", "POST", "/api/passkey/register-options", body={},
                               headers={"Cookie": f"{SESSION_COOKIE}=session"}, handler_type=h)
    assert status == 200
    status, _ = call_handler(store, settings, "secret", "POST", "/api/passkey/register",
                             body=dev.create(json.loads(raw)["challenge"]),
                             headers={"Cookie": f"{SESSION_COOKIE}=session"}, handler_type=h)
    assert status == 200
    status, raw = call_handler(store, settings, "secret", "POST", "/session/passkey-options", body={}, handler_type=h)
    assert status == 200
    status, _ = call_handler(store, settings, "secret", "POST", "/session/passkey",
                             body=dev.get(json.loads(raw)["challenge"]), handler_type=h)
    assert status == 204 and len(h.sessions) == 2                       # nová session vedle té testovací
    actions = [e["action"] for e in store.history(limit=10)]
    assert "web-passkey-added" in actions and "web-login-passkey" in actions


def test_registration_needs_session_and_login_needs_local_origin(tmp_path: Path):
    settings, store = handler_env(tmp_path)
    h = make_handler(store, settings, "secret", 8765)
    assert call_handler(store, settings, "secret", "POST", "/api/passkey/register-options", body={},
                        handler_type=h)[0] == 403
    assert call_handler(store, settings, "secret", "POST", "/session/passkey-options", body={},
                        headers={"Origin": "http://evil.example"}, handler_type=h)[0] == 403


def test_failed_logins_are_rate_limited(tmp_path: Path):
    settings, store = handler_env(tmp_path)
    h = make_handler(store, settings, "secret", 8765)
    for _ in range(10):
        assert call_handler(store, settings, "secret", "POST", "/session/passkey", body={"id": "x"},
                            handler_type=h)[0] == 403
    assert call_handler(store, settings, "secret", "POST", "/session/passkey-options", body={},
                        handler_type=h)[0] == 429
    assert any(e["action"] == "web-login-passkey-failed" for e in store.history(limit=20))
    h.passkeys._failures = [time.monotonic() - 400] * 10                  # po 5 minutách se odblokuje
    assert not h.passkeys.blocked()
