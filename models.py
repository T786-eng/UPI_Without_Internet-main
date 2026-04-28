"""SQLAlchemy ORM models for Account and Transaction."""
from decimal import Decimal
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Account(db.Model):
    """Simulated bank account with optimistic locking."""
    __tablename__ = "accounts"

    vpa = db.Column(db.String(64), primary_key=True)
    holder_name = db.Column(db.String(128), nullable=False)
    balance = db.Column(db.Numeric(19, 2), nullable=False, default=Decimal("0.00"))
    # Optimistic locking — prevents lost updates on concurrent transfers
    version = db.Column(db.Integer, nullable=False, default=0)

    __mapper_args__ = {"version_id_col": version}

    def __init__(self, vpa: str, holder_name: str, balance: Decimal):
        self.vpa = vpa
        self.holder_name = holder_name
        self.balance = balance

    def to_dict(self):
        return {
            "vpa": self.vpa,
            "holderName": self.holder_name,
            "balance": str(self.balance),
        }


class Transaction(db.Model):
    """Permanent record of every settled transaction."""
    __tablename__ = "transactions"
    __table_args__ = (db.Index("idx_packet_hash", "packet_hash", unique=True),)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    packet_hash = db.Column(db.String(64), nullable=False, unique=True)
    sender_vpa = db.Column(db.String(64), nullable=False)
    receiver_vpa = db.Column(db.String(64), nullable=False)
    amount = db.Column(db.Numeric(19, 2), nullable=False)
    signed_at = db.Column(db.DateTime, nullable=False)
    settled_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    bridge_node_id = db.Column(db.String(64), nullable=False)
    hop_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(16), nullable=False, default="SETTLED")

    def __init__(self, packet_hash: str, sender_vpa: str, receiver_vpa: str,
                 amount: Decimal, signed_at: datetime, settled_at: datetime,
                 bridge_node_id: str, hop_count: int, status: str):
        self.packet_hash = packet_hash
        self.sender_vpa = sender_vpa
        self.receiver_vpa = receiver_vpa
        self.amount = amount
        self.signed_at = signed_at
        self.settled_at = settled_at
        self.bridge_node_id = bridge_node_id
        self.hop_count = hop_count
        self.status = status

    def to_dict(self):
        return {
            "id": self.id,
            "packetHash": self.packet_hash,
            "senderVpa": self.sender_vpa,
            "receiverVpa": self.receiver_vpa,
            "amount": str(self.amount),
            "signedAt": self.signed_at.isoformat() if self.signed_at else None,
            "settledAt": self.settled_at.isoformat() if self.settled_at else None,
            "bridgeNodeId": self.bridge_node_id,
            "hopCount": self.hop_count,
            "status": self.status,
        }

