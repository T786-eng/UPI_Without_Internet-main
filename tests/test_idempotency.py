"""Pytest suite for idempotency and crypto correctness."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app import create_app
from models import db, Account, Transaction
from crypto_service import HybridCryptoService, ServerKeyHolder


@pytest.fixture
def app():
    """Create application for testing."""
    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    })
    with app.app_context():
        db.create_all()
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def services(app):
    """Expose wired services from the app context."""
    with app.app_context():
        from services import (
            DemoService, MeshSimulatorService, BridgeIngestionService,
            IdempotencyService, SettlementService,
        )
        from crypto_service import HybridCryptoService, ServerKeyHolder

        server_key = ServerKeyHolder()
        crypto = HybridCryptoService(server_key)
        idempotency = IdempotencyService()
        settlement = SettlementService()
        bridge = BridgeIngestionService(crypto, idempotency, settlement)
        mesh = MeshSimulatorService()
        demo = DemoService(crypto, server_key)
        demo.seed_accounts()

        return {
            "demo": demo,
            "bridge": bridge,
            "idempotency": idempotency,
            "crypto": crypto,
            "server_key": server_key,
        }


def test_single_packet_delivered_by_three_bridges_settles_exactly_once(app, services):
    """3 bridges deliver the same packet concurrently — only one settles."""
    with app.app_context():
        alice_before = db.session.get(Account, "alice@demo").balance
        bob_before = db.session.get(Account, "bob@demo").balance

        packet = services["demo"].create_packet(
            "alice@demo", "bob@demo", Decimal("100.00"), "1234", 5)

    barrier = threading.Barrier(3)
    settled = threading.Lock()
    settled_count = [0]
    duplicate_count = [0]

    def upload(node_id: str):
        with app.app_context():
            barrier.wait()
            r = services["bridge"].ingest(packet, node_id, 3)
        if r.outcome == "SETTLED":
            with settled:
                settled_count[0] += 1
        elif r.outcome == "DUPLICATE_DROPPED":
            with settled:
                duplicate_count[0] += 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(upload, f"bridge-{i}") for i in range(3)]
        for f in futures:
            f.result(timeout=5)

    assert settled_count[0] == 1, "exactly one bridge should settle"
    assert duplicate_count[0] == 2, "the other two should be duplicates"

    with app.app_context():
        alice_after = db.session.get(Account, "alice@demo").balance
        bob_after = db.session.get(Account, "bob@demo").balance
        assert alice_after == alice_before - Decimal("100.00")
        assert bob_after == bob_before + Decimal("100.00")


def test_tampered_ciphertext_is_rejected(app, services):
    with app.app_context():
        packet = services["demo"].create_packet(
            "alice@demo", "bob@demo", Decimal("50.00"), "1234", 5)

        # Flip a byte in the middle of the ciphertext
        chars = list(packet["ciphertext"])
        mid = len(chars) // 2
        chars[mid] = "B" if chars[mid] == "A" else "A"
        packet["ciphertext"] = "".join(chars)

        r = services["bridge"].ingest(packet, "bridge-x", 1)
        assert r.outcome == "INVALID"


def test_encrypt_decrypt_round_trip(services):
    original = {
        "senderVpa": "alice@demo",
        "receiverVpa": "bob@demo",
        "amount": "123.45",
        "pinHash": "abcdef",
        "nonce": "nonce-1",
        "signedAt": int(time.time() * 1000),
    }

    ct = services["crypto"].encrypt(original, services["server_key"].public_key)
    decrypted = services["crypto"].decrypt(ct)

    assert decrypted["senderVpa"] == original["senderVpa"]
    assert decrypted["receiverVpa"] == original["receiverVpa"]
    assert Decimal(decrypted["amount"]) == Decimal(original["amount"])
    assert decrypted["nonce"] == original["nonce"]
