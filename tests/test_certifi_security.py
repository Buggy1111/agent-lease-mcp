"""Regression for GHSA-6q48-6c7w-9gw2: remove the compromised emSign root."""
import hashlib
import re
import ssl
from pathlib import Path

import certifi


def test_compromised_emsign_root_is_not_trusted():
    bundle = Path(certifi.where()).read_bytes()
    blocks = re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle, re.DOTALL)
    assert blocks
    digests = {hashlib.sha256(ssl.PEM_cert_to_DER_cert(block.decode())).hexdigest() for block in blocks}
    # SHA-256 fingerprint of emSign ECC Root CA - G3, independent of bundle comments.
    assert "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b" not in digests
