"""UPI Offline Mesh — Backend Application."""

import atexit
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

import config
from models import db, Account, Transaction
from crypto_service import ServerKeyHolder, HybridCryptoService
from services import (
    DemoService,
    MeshSimulatorService,
    BridgeIngestionService,
    IdempotencyService,
    SettlementService,
)
from controllers import api_bp, dash_bp, init_controllers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(threadName)s] %(levelname)-5s %(name)-36s - %(message)s",
    datefmt="%H:%M:%S",
)


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config.from_object(config)

    db.init_app(app)

    with app.app_context():
        db.create_all()

        server_key = ServerKeyHolder()
        crypto = HybridCryptoService(server_key)
        idempotency = IdempotencyService()
        settlement = SettlementService()
        bridge = BridgeIngestionService(crypto, idempotency, settlement)
        mesh = MeshSimulatorService()
        demo = DemoService(crypto, server_key)

        demo.seed_accounts()

        init_controllers(server_key, demo, mesh, bridge, idempotency)

    app.register_blueprint(api_bp)
    app.register_blueprint(dash_bp)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        idempotency.evict_expired,
        "interval",
        seconds=60,
        id="evict_expired",
        replace_existing=True,
    )
    scheduler.start()

    atexit.register(lambda: scheduler.shutdown())

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.SERVER_PORT, debug=True)
