"""Business logic services."""

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from models import db, Account, Transaction
from crypto_service import HybridCryptoService, ServerKeyHolder
import config


class IdempotencyService:
    """In-memory idempotency cache."""

    def __init__(self):
        self._seen: Dict[str, datetime] = {}
        self._lock = threading.Lock()

    def claim(self, packet_hash: str) -> bool:
        """Try to claim a hash. Returns True if this caller is the first."""
        now = datetime.now(timezone.utc)
        with self._lock:
            if packet_hash in self._seen:
                return False
            self._seen[packet_hash] = now
            return True

    def size(self) -> int:
        with self._lock:
            return len(self._seen)

    def evict_expired(self):
        """Evict entries past their TTL."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.IDEMPOTENCY_TTL_SECONDS)
        with self._lock:
            expired = [k for k, v in self._seen.items() if v < cutoff]
            for k in expired:
                del self._seen[k]

    def clear(self):
        with self._lock:
            self._seen.clear()


class VirtualDevice:
    """A simulated phone in the mesh. Holds packets it has seen."""

    def __init__(self, device_id: str, has_internet: bool):
        self.device_id = device_id
        self.has_internet = has_internet
        self._held_packets: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def hold(self, packet: dict):
        with self._lock:
            self._held_packets.setdefault(packet["packetId"], packet)

    def get_held_packets(self) -> List[dict]:
        with self._lock:
            return list(self._held_packets.values())

    def holds(self, packet_id: str) -> bool:
        with self._lock:
            return packet_id in self._held_packets

    def packet_count(self) -> int:
        with self._lock:
            return len(self._held_packets)

    def clear(self):
        with self._lock:
            self._held_packets.clear()


@dataclass
class GossipResult:
    transfers: int
    device_counts: Dict[str, int]


@dataclass
class BridgeUpload:
    bridge_node_id: str
    packet: dict


class MeshSimulatorService:
    """Simulates the Bluetooth mesh."""

    log = logging.getLogger("services.MeshSimulatorService")

    def __init__(self):
        self._devices: Dict[str, VirtualDevice] = {}
        self._lock = threading.Lock()
        self.seed_default_devices()

    def seed_default_devices(self):
        with self._lock:
            self._devices = {
                "phone-alice": VirtualDevice("phone-alice", False),
                "phone-stranger1": VirtualDevice("phone-stranger1", False),
                "phone-stranger2": VirtualDevice("phone-stranger2", False),
                "phone-stranger3": VirtualDevice("phone-stranger3", False),
                "phone-bridge": VirtualDevice("phone-bridge", True),
            }

    def get_devices(self) -> List[VirtualDevice]:
        with self._lock:
            return list(self._devices.values())

    def get_device(self, device_id: str) -> Optional[VirtualDevice]:
        with self._lock:
            return self._devices.get(device_id)

    def inject(self, sender_device_id: str, packet: dict):
        sender = self.get_device(sender_device_id)
        if sender is None:
            raise ValueError(f"Unknown device: {sender_device_id}")
        sender.hold(packet)
        self.log.info(
            "Packet %s injected at %s (TTL=%s)",
            packet["packetId"][:8], sender_device_id, packet["ttl"]
        )

    def gossip_once(self) -> GossipResult:
        with self._lock:
            device_list = list(self._devices.values())

            snapshot: Dict[str, List[dict]] = {}
            for d in device_list:
                snapshot[d.device_id] = d.get_held_packets()

            transfers = 0
            for src in device_list:
                for pkt in snapshot[src.device_id]:
                    if pkt["ttl"] <= 0:
                        continue
                    for dst in device_list:
                        if dst.device_id == src.device_id:
                            continue
                        if dst.holds(pkt["packetId"]):
                            continue
                        copy = {
                            "packetId": pkt["packetId"],
                            "ttl": pkt["ttl"] - 1,
                            "createdAt": pkt["createdAt"],
                            "ciphertext": pkt["ciphertext"],
                        }
                        dst.hold(copy)
                        transfers += 1

            self.log.info("Gossip round complete: %s packet transfers", transfers)
            return GossipResult(transfers, self._snapshot_map())

    def _snapshot_map(self) -> Dict[str, int]:
        result = {}
        for d in self._devices.values():
            result[d.device_id] = d.packet_count()
        return result

    def collect_bridge_uploads(self) -> List[BridgeUpload]:
        out = []
        for d in self.get_devices():
            if not d.has_internet:
                continue
            for pkt in d.get_held_packets():
                out.append(BridgeUpload(d.device_id, pkt))
        return out

    def reset_mesh(self):
        with self._lock:
            for d in self._devices.values():
                d.clear()


@dataclass
class IngestResult:
    outcome: str
    packet_hash: str
    reason: Optional[str] = None
    transaction_id: Optional[int] = None

    @staticmethod
    def settled(packet_hash: str, tx: Transaction) -> "IngestResult":
        return IngestResult("SETTLED", packet_hash, None, tx.id)

    @staticmethod
    def duplicate(packet_hash: str) -> "IngestResult":
        return IngestResult("DUPLICATE_DROPPED", packet_hash, None, None)

    @staticmethod
    def invalid(packet_hash: str, reason: str) -> "IngestResult":
        return IngestResult("INVALID", packet_hash, reason, None)


class SettlementService:
    """Where the actual ledger update happens."""

    log = logging.getLogger("services.SettlementService")

    def settle(self, instruction: dict, packet_hash: str,
               bridge_node_id: str, hop_count: int) -> Transaction:
        sender_vpa = instruction["senderVpa"]
        receiver_vpa = instruction["receiverVpa"]
        amount = Decimal(str(instruction["amount"]))

        if amount <= Decimal("0"):
            raise ValueError("Amount must be positive")

        sender = db.session.get(Account, sender_vpa)
        if sender is None:
            raise ValueError(f"Unknown sender VPA: {sender_vpa}")

        receiver = db.session.get(Account, receiver_vpa)
        if receiver is None:
            raise ValueError(f"Unknown receiver VPA: {receiver_vpa}")

        if sender.balance < amount:
            self.log.warning(
                "Insufficient balance: %s has ₹%s, tried to send ₹%s",
                sender.vpa, sender.balance, amount
            )
            return self._record_rejected(instruction, packet_hash, bridge_node_id, hop_count)

        sender.balance = sender.balance - amount
        receiver.balance = receiver.balance + amount
        db.session.add(sender)
        db.session.add(receiver)

        tx = Transaction(
            packet_hash=packet_hash,
            sender_vpa=sender_vpa,
            receiver_vpa=receiver_vpa,
            amount=amount,
            signed_at=datetime.fromtimestamp(instruction["signedAt"] / 1000, tz=timezone.utc),
            settled_at=datetime.now(timezone.utc),
            bridge_node_id=bridge_node_id,
            hop_count=hop_count,
            status="SETTLED",
        )
        db.session.add(tx)
        db.session.commit()

        self.log.info(
            "SETTLED ₹%s from %s to %s (packetHash=%s..., bridge=%s, hops=%s)",
            amount, sender.vpa, receiver.vpa,
            packet_hash[:12], bridge_node_id, hop_count
        )
        return tx

    def _record_rejected(self, instruction: dict, packet_hash: str,
                         bridge_node_id: str, hop_count: int) -> Transaction:
        tx = Transaction(
            packet_hash=packet_hash,
            sender_vpa=instruction["senderVpa"],
            receiver_vpa=instruction["receiverVpa"],
            amount=Decimal(str(instruction["amount"])),
            signed_at=datetime.fromtimestamp(instruction["signedAt"] / 1000, tz=timezone.utc),
            settled_at=datetime.now(timezone.utc),
            bridge_node_id=bridge_node_id,
            hop_count=hop_count,
            status="REJECTED",
        )
        db.session.add(tx)
        db.session.commit()
        return tx


class BridgeIngestionService:
    """Orchestrates the full server-side pipeline for one inbound packet."""

    log = logging.getLogger("services.BridgeIngestionService")

    def __init__(self, crypto: HybridCryptoService,
                 idempotency: IdempotencyService,
                 settlement: SettlementService):
        self.crypto = crypto
        self.idempotency = idempotency
        self.settlement = settlement

    def ingest(self, packet: dict, bridge_node_id: str, hop_count: int) -> IngestResult:
        try:
            packet_hash = self.crypto.hash_ciphertext(packet["ciphertext"])

            if not self.idempotency.claim(packet_hash):
                self.log.info(
                    "DUPLICATE packet %s... from bridge %s — dropped",
                    packet_hash[:12], bridge_node_id
                )
                return IngestResult.duplicate(packet_hash)

            try:
                instruction = self.crypto.decrypt(packet["ciphertext"])
            except Exception as e:
                self.log.warning(
                    "Decryption failed for packet %s...: %s",
                    packet_hash[:12], e
                )
                return IngestResult.invalid(packet_hash, "decryption_failed")

            age_seconds = (time.time() * 1000 - instruction["signedAt"]) / 1000
            if age_seconds > config.PACKET_MAX_AGE_SECONDS:
                self.log.warning(
                    "Packet %s... too old (%ss), rejected",
                    packet_hash[:12], age_seconds
                )
                return IngestResult.invalid(packet_hash, "stale_packet")
            if age_seconds < -300:
                return IngestResult.invalid(packet_hash, "future_dated")

            tx = self.settlement.settle(instruction, packet_hash, bridge_node_id, hop_count)
            return IngestResult.settled(packet_hash, tx)

        except Exception as e:
            self.log.error("Ingestion error: %s", e)
            return IngestResult.invalid("?", f"internal_error: {e}")


class DemoService:
    """Helper service that seeds demo accounts and simulates sender phone."""

    log = logging.getLogger("services.DemoService")

    def __init__(self, crypto: HybridCryptoService, server_key: ServerKeyHolder):
        self.crypto = crypto
        self.server_key = server_key

    def seed_accounts(self):
        if Account.query.count() == 0:
            db.session.add(Account("alice@demo", "Alice", Decimal("5000.00")))
            db.session.add(Account("bob@demo", "Bob", Decimal("1000.00")))
            db.session.add(Account("carol@demo", "Carol", Decimal("2500.00")))
            db.session.add(Account("dave@demo", "Dave", Decimal("500.00")))
            db.session.commit()
            self.log.info("Seeded 4 demo accounts")

    def create_packet(self, sender_vpa: str, receiver_vpa: str,
                      amount: Decimal, pin: str, ttl: int) -> dict:
        instruction = {
            "senderVpa": sender_vpa,
            "receiverVpa": receiver_vpa,
            "amount": str(amount),
            "pinHash": self._sha256_hex(pin),
            "nonce": str(uuid.uuid4()),
            "signedAt": int(time.time() * 1000),
        }

        ciphertext = self.crypto.encrypt(instruction, self.server_key.public_key)

        packet = {
            "packetId": str(uuid.uuid4()),
            "ttl": ttl,
            "createdAt": int(time.time() * 1000),
            "ciphertext": ciphertext,
        }
        return packet

    @staticmethod
    def _sha256_hex(input_str: str) -> str:
        return hashlib.sha256(input_str.encode("utf-8")).hexdigest()
