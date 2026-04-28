"""Application configuration."""

import os

SERVER_PORT = int(os.environ.get("PORT", 8080))

SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL", "sqlite:///upimesh.db"
)
SQLALCHEMY_ENGINE_OPTIONS = {
    "connect_args": {"check_same_thread": False},
    "pool_pre_ping": True,
}
SQLALCHEMY_TRACK_MODIFICATIONS = False

IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", 86400))
PACKET_MAX_AGE_SECONDS = int(os.environ.get("PACKET_MAX_AGE_SECONDS", 86400))
