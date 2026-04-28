"""Hybrid encryption — RSA-OAEP + AES-256-GCM.

Wire format (after base64 encoding):
  [ 256 bytes RSA-encrypted AES key ][ 12 bytes GCM IV ][ ciphertext + 16-byte tag ]
"""

import base64
import hashlib
import json
import logging
import os
import threading
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ServerKeyHolder:
    """Holds the server's RSA keypair."""

    _instance: Optional["ServerKeyHolder"] = None
    _lock = threading.Lock()
    log = logging.getLogger("crypto_service.ServerKeyHolder")

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self._private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self._public_key = self._private_key.public_key()
        pub_b64 = self.public_key_base64
        self.log.info(
            "Server RSA keypair generated (2048-bit). Public key fingerprint: %s...",
            pub_b64[:32]
        )

    @property
    def private_key(self):
        return self._private_key

    @property
    def public_key(self):
        return self._public_key

    @property
    def public_key_base64(self) -> str:
        pem = self._public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(pem).decode("ascii")


class HybridCryptoService:
    """Encrypt/decrypt PaymentInstruction objects using hybrid RSA+AES."""

    RSA_ENCRYPTED_KEY_BYTES = 256
    GCM_IV_BYTES = 12
    GCM_TAG_BITS = 128

    def __init__(self, server_key: Optional[ServerKeyHolder] = None):
        self.server_key = server_key or ServerKeyHolder()

    def encrypt(self, instruction: dict, public_key) -> str:
        """Encrypt a payment instruction with the server's public key."""
        plaintext = json.dumps(instruction, separators=(",", ":")).encode("utf-8")

        aes_key = AESGCM.generate_key(bit_length=256)
        aesgcm = AESGCM(aes_key)

        iv = os.urandom(self.GCM_IV_BYTES)
        aes_ciphertext = aesgcm.encrypt(iv, plaintext, None)

        encrypted_aes_key = public_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        wire = encrypted_aes_key + iv + aes_ciphertext
        return base64.b64encode(wire).decode("ascii")

    def decrypt(self, base64_ciphertext: str) -> dict:
        """Decrypt with the server's private key."""
        all_bytes = base64.b64decode(base64_ciphertext)

        min_len = self.RSA_ENCRYPTED_KEY_BYTES + self.GCM_IV_BYTES + (self.GCM_TAG_BITS // 8)
        if len(all_bytes) < min_len:
            raise ValueError("Ciphertext too short")

        encrypted_aes_key = all_bytes[: self.RSA_ENCRYPTED_KEY_BYTES]
        iv = all_bytes[self.RSA_ENCRYPTED_KEY_BYTES : self.RSA_ENCRYPTED_KEY_BYTES + self.GCM_IV_BYTES]
        aes_ciphertext = all_bytes[self.RSA_ENCRYPTED_KEY_BYTES + self.GCM_IV_BYTES :]

        aes_key = self.server_key.private_key.decrypt(
            encrypted_aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(iv, aes_ciphertext, None)

        return json.loads(plaintext.decode("utf-8"))

    def hash_ciphertext(self, base64_ciphertext: str) -> str:
        """SHA-256 of the ciphertext. THIS is the idempotency key."""
        return hashlib.sha256(base64_ciphertext.encode("utf-8")).hexdigest()
