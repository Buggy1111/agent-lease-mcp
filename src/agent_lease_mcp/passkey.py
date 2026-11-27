"""Přihlášení do webu přes Windows Hello (WebAuthn, PIN) — jen loopback, jeden člověk.

Server ověřuje jen to, co bezpečnost opravdu potřebuje (attestation „none“):
jednorázovou výzvu, typ a původ v clientDataJSON, hash RP ID, příznaky
uživatel přítomen + ověřen (PIN), podpis uloženým veřejným klíčem a čítač.
Klíče leží vedle DB v souboru 0600; tajemství z nich nejde odvodit.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import time
from pathlib import Path
from threading import Lock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

RP_ID = "localhost"            # WebAuthn nepovoluje IP adresu jako RP ID, proto jen http://localhost:<port>
CHALLENGE_SECONDS = 120
MAX_KEYS = 10
PASSKEY_FILE = "webui.passkeys.json"
ALG_ES256, ALG_RS256 = -7, -257
FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


class PasskeyError(ValueError):
    """Ověření neprošlo; zpráva je bezpečná pro klienta (neobsahuje tajemství)."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    if not isinstance(text, str) or len(text) > 8192:
        raise PasskeyError("invalid encoding")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise PasskeyError("invalid encoding") from exc


# ── minimální CBOR (RFC 8949) jen pro attestationObject a COSE klíč ──────────
def cbor_decode(data: bytes):
    value, end = _cbor(data, 0, 0)
    if end != len(data):
        raise PasskeyError("trailing CBOR data")
    return value


def _cbor(data: bytes, pos: int, depth: int):
    if depth > 8 or pos >= len(data):
        raise PasskeyError("invalid CBOR")
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise PasskeyError("invalid CBOR")
        arg = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    else:
        raise PasskeyError("unsupported CBOR")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise PasskeyError("invalid CBOR")
        raw = data[pos:pos + arg]
        return (raw if major == 2 else raw.decode()), pos + arg
    if major in (4, 5):
        if arg > 64:
            raise PasskeyError("CBOR too large")
        if major == 4:
            items = []
            for _ in range(arg):
                item, pos = _cbor(data, pos, depth + 1)
                items.append(item)
            return items, pos
        out = {}
        for _ in range(arg):
            key, pos = _cbor(data, pos, depth + 1)
            val, pos = _cbor(data, pos, depth + 1)
            if not isinstance(key, (int, str)):
                raise PasskeyError("invalid CBOR map key")
            out[key] = val
        return out, pos
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    raise PasskeyError("unsupported CBOR")


def cose_public_key(cose: dict) -> tuple[int, bytes]:
    """COSE klíč → (alg, veřejný klíč ve formátu DER SubjectPublicKeyInfo)."""
    if not isinstance(cose, dict):
        raise PasskeyError("invalid key")
    alg = cose.get(3)
    if alg == ALG_ES256 and cose.get(1) == 2 and cose.get(-1) == 1:
        x, y = cose.get(-2), cose.get(-3)
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
            raise PasskeyError("invalid EC key")
        key = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                            ec.SECP256R1()).public_key()
    elif alg == ALG_RS256 and cose.get(1) == 3:
        n, e = cose.get(-1), cose.get(-2)
        if not (isinstance(n, bytes) and isinstance(e, bytes) and 256 <= len(n) <= 512):
            raise PasskeyError("invalid RSA key")
        key = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
    else:
        raise PasskeyError("unsupported key algorithm")
    return alg, key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _verify_signature(alg: int, der: bytes, signature: bytes, signed: bytes) -> None:
    key = serialization.load_der_public_key(der)
    try:
        if alg == ALG_ES256 and isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == ALG_RS256 and isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise PasskeyError("unsupported key algorithm")
    except InvalidSignature as exc:
        raise PasskeyError("bad signature") from exc


def _auth_data(data: bytes) -> tuple[int, int, bytes]:
    if len(data) < 37:
        raise PasskeyError("invalid authenticator data")
    if data[:32] != hashlib.sha256(RP_ID.encode()).digest():
        raise PasskeyError("wrong relying party")
    flags, count = data[32], struct.unpack(">I", data[33:37])[0]
    if not flags & FLAG_UP or not flags & FLAG_UV:
        raise PasskeyError("user verification (PIN) required")
    return flags, count, data[37:]


class Passkeys:
    """Uložené klíče + jednorázové výzvy. Jedna instance na proces webu."""

    def __init__(self, directory: Path, allowed_origins: set[str]) -> None:
        self.path = directory / PASSKEY_FILE
        self.origins = {o for o in allowed_origins if o.startswith("http://localhost:")}
        self._challenges: dict[str, tuple[str, float]] = {}
        self._lock = Lock()
        self._failures: list[float] = []

    # ── úložiště ──
    def keys(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, keys: list[dict]) -> None:
        tmp = self.path.with_name(f"{PASSKEY_FILE}.{secrets.token_hex(8)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(keys, handle)
        os.replace(tmp, self.path)

    def public_list(self) -> list[dict]:
        return [{"id": k["id"], "name": k["name"], "created_at": k["created_at"], "last_used": k.get("last_used")}
                for k in self.keys()]

    def remove(self, key_id: str) -> bool:
        with self._lock:
            keys = self.keys()
            kept = [k for k in keys if k["id"] != key_id]
            if len(kept) == len(keys):
                return False
            self._save(kept)
            return True

    # ── výzvy a omezení pokusů ──
    def challenge(self, purpose: str) -> str:
        now = time.monotonic()
        with self._lock:
            for c in [c for c, (_, exp) in self._challenges.items() if exp <= now]:
                del self._challenges[c]
            # Oddělené kapacity (review #59): nepřihlášené pokusy nesmí zablokovat registraci klíče.
            if sum(1 for p, _ in self._challenges.values() if p == purpose) >= 32:
                raise PasskeyError("too many pending challenges")
            value = b64u(secrets.token_bytes(32))
            self._challenges[value] = (purpose, now + CHALLENGE_SECONDS)
            return value

    def _take_challenge(self, value: str, purpose: str) -> None:
        with self._lock:
            entry = self._challenges.pop(value, None)
        if not entry or entry[0] != purpose or entry[1] <= time.monotonic():
            raise PasskeyError("unknown or expired challenge")

    def blocked(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._failures = [t for t in self._failures if now - t < 300]
            return len(self._failures) >= 10

    def failed(self) -> None:
        with self._lock:
            self._failures.append(time.monotonic())

    def _client_data(self, raw_b64: str, kind: str, purpose: str) -> bytes:
        raw = unb64u(raw_b64)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PasskeyError("invalid client data") from exc
        if not isinstance(data, dict) or data.get("type") != kind:
            raise PasskeyError("wrong ceremony type")
        if not isinstance(data.get("origin"), str) or data["origin"] not in self.origins:
            raise PasskeyError("wrong origin")
        if data.get("crossOrigin"):
            raise PasskeyError("cross-origin not allowed")
        if not isinstance(data.get("challenge"), str):
            raise PasskeyError("invalid challenge")
        self._take_challenge(data["challenge"], purpose)
        return raw

    # ── registrace (jen s platným přihlášením) ──
    def register_options(self, user_name: str) -> dict:
        return {"challenge": self.challenge("register"), "rp": {"id": RP_ID, "name": "Agent Lease"},
                "user": {"id": b64u(hashlib.sha256(b"agent-lease-human").digest()[:16]), "name": user_name,
                         "displayName": user_name},
                "pubKeyCredParams": [{"type": "public-key", "alg": ALG_ES256}, {"type": "public-key", "alg": ALG_RS256}],
                "authenticatorSelection": {"authenticatorAttachment": "platform", "userVerification": "required",
                                           "residentKey": "preferred"},
                "attestation": "none", "timeout": CHALLENGE_SECONDS * 1000,
                "excludeCredentials": [{"type": "public-key", "id": k["id"]} for k in self.keys()]}

    def register(self, body: dict) -> dict:
        try:
            return self._register(body)
        except PasskeyError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, AttributeError, struct.error) as exc:
            raise PasskeyError("malformed registration") from exc

    def _register(self, body: dict) -> dict:
        client_raw = self._client_data(body.get("clientDataJSON", ""), "webauthn.create", "register")
        del client_raw
        att = cbor_decode(unb64u(body.get("attestationObject", "")))
        if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
            raise PasskeyError("invalid attestation")
        flags, count, rest = _auth_data(att["authData"])
        if not flags & FLAG_AT or len(rest) < 18:
            raise PasskeyError("no credential in attestation")
        id_len = struct.unpack(">H", rest[16:18])[0]
        cred_id = rest[18:18 + id_len]
        if not 16 <= id_len <= 1023 or len(cred_id) != id_len:
            raise PasskeyError("invalid credential id")
        cose, _end = _cbor(rest, 18 + id_len, 0)
        alg, der = cose_public_key(cose)
        name = str(body.get("name") or "Windows Hello").strip()[:60] or "Windows Hello"
        with self._lock:
            keys = self.keys()
            if len(keys) >= MAX_KEYS:
                raise PasskeyError("too many keys")
            if any(k["id"] == b64u(cred_id) for k in keys):
                raise PasskeyError("key already registered")
            entry = {"id": b64u(cred_id), "alg": alg, "public_key": b64u(der), "sign_count": count,
                     "name": name, "created_at": time.time()}
            self._save([*keys, entry])
        return {"id": entry["id"], "name": name}

    # ── přihlášení ──
    def login_options(self) -> dict:
        keys = self.keys()
        return {"challenge": self.challenge("login") if keys else "", "rpId": RP_ID, "userVerification": "required",
                "timeout": CHALLENGE_SECONDS * 1000,
                "allowCredentials": [{"type": "public-key", "id": k["id"]} for k in keys]}

    def login(self, body: dict) -> dict:
        try:
            return self._login(body)
        except PasskeyError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, AttributeError, struct.error) as exc:
            raise PasskeyError("malformed login") from exc

    def _login(self, body: dict) -> dict:
        key_id = body.get("id", "")
        client_raw = self._client_data(body.get("clientDataJSON", ""), "webauthn.get", "login")
        auth = unb64u(body.get("authenticatorData", ""))
        _flags, count, _rest = _auth_data(auth)
        with self._lock:
            keys = self.keys()
            key = next((k for k in keys if k["id"] == key_id), None)
            if key is None:
                raise PasskeyError("unknown key")
            _verify_signature(key["alg"], unb64u(key["public_key"]), unb64u(body.get("signature", "")),
                              auth + hashlib.sha256(client_raw).digest())
            if (count or key["sign_count"]) and count <= key["sign_count"]:
                raise PasskeyError("signature counter did not increase (cloned key?)")
            key["sign_count"], key["last_used"] = count, time.time()
            self._save(keys)
        return {"id": key["id"], "name": key["name"]}
