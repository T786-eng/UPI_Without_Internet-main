"""REST API controllers."""

from decimal import Decimal
from flask import Blueprint, jsonify, request, render_template, current_app

from crypto_service import ServerKeyHolder, HybridCryptoService
from models import Account, Transaction
from services import (
    DemoService, MeshSimulatorService, BridgeIngestionService,
    IdempotencyService, SettlementService,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")

server_key: ServerKeyHolder = None
demo_service: DemoService = None
mesh_service: MeshSimulatorService = None
bridge_service: BridgeIngestionService = None
idempotency_service: IdempotencyService = None


def init_controllers(sk: ServerKeyHolder, ds: DemoService, ms: MeshSimulatorService,
                     bs: BridgeIngestionService, ids: IdempotencyService):
    global server_key, demo_service, mesh_service, bridge_service, idempotency_service
    server_key = sk
    demo_service = ds
    mesh_service = ms
    bridge_service = bs
    idempotency_service = ids


@api_bp.route("/server-key", methods=["GET"])
def get_server_public_key():
    return jsonify({
        "publicKey": server_key.public_key_base64,
        "algorithm": "RSA-2048 / OAEP-SHA256",
        "hybridScheme": "RSA-OAEP encrypts an AES-256-GCM session key",
    })


@api_bp.route("/demo/send", methods=["POST"])
def demo_send():
    req = request.get_json(force=True)
    sender_vpa = req.get("senderVpa")
    receiver_vpa = req.get("receiverVpa")
    amount = Decimal(str(req.get("amount")))
    pin = req.get("pin")
    ttl = req.get("ttl", 5)
    start_device = req.get("startDevice", "phone-alice")

    packet = demo_service.create_packet(sender_vpa, receiver_vpa, amount, pin, ttl)
    mesh_service.inject(start_device, packet)

    return jsonify({
        "packetId": packet["packetId"],
        "ciphertextPreview": packet["ciphertext"][:64] + "...",
        "ttl": packet["ttl"],
        "injectedAt": start_device,
    })


@api_bp.route("/mesh/state", methods=["GET"])
def mesh_state():
    device_data = []
    for d in mesh_service.get_devices():
        device_data.append({
            "deviceId": d.device_id,
            "hasInternet": d.has_internet,
            "packetCount": d.packet_count(),
            "packetIds": [p["packetId"][:8] for p in d.get_held_packets()],
        })
    return jsonify({
        "devices": device_data,
        "idempotencyCacheSize": idempotency_service.size(),
    })


@api_bp.route("/mesh/gossip", methods=["POST"])
def mesh_gossip():
    result = mesh_service.gossip_once()
    return jsonify({
        "transfers": result.transfers,
        "deviceCounts": result.device_counts,
    })


@api_bp.route("/mesh/flush", methods=["POST"])
def mesh_flush():
    uploads = mesh_service.collect_bridge_uploads()
    results = []
    results_lock = __import__("threading").Lock()

    def upload_one(up):
        with current_app.app_context():
            r = bridge_service.ingest(up.packet, up.bridge_node_id, 5 - up.packet["ttl"])
        with results_lock:
            results.append({
                "bridgeNode": up.bridge_node_id,
                "packetId": up.packet["packetId"][:8],
                "outcome": r.outcome,
                "reason": r.reason or "",
                "transactionId": r.transaction_id if r.transaction_id is not None else -1,
            })

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(upload_one, uploads)

    return jsonify({
        "uploadsAttempted": len(uploads),
        "results": results,
    })


@api_bp.route("/mesh/reset", methods=["POST"])
def mesh_reset():
    mesh_service.reset_mesh()
    idempotency_service.clear()
    return jsonify({"status": "mesh and idempotency cache cleared"})


@api_bp.route("/bridge/ingest", methods=["POST"])
def ingest():
    packet = request.get_json(force=True)
    bridge_node_id = request.headers.get("X-Bridge-Node-Id", "unknown")
    hop_count = int(request.headers.get("X-Hop-Count", 0))
    r = bridge_service.ingest(packet, bridge_node_id, hop_count)
    return jsonify({
        "outcome": r.outcome,
        "packetHash": r.packet_hash,
        "reason": r.reason,
        "transactionId": r.transaction_id,
    })


@api_bp.route("/accounts", methods=["GET"])
def list_accounts():
    accounts = Account.query.all()
    return jsonify([a.to_dict() for a in accounts])


@api_bp.route("/transactions", methods=["GET"])
def list_transactions():
    txs = Transaction.query.order_by(Transaction.id.desc()).limit(20).all()
    return jsonify([t.to_dict() for t in txs])


dash_bp = Blueprint("dashboard", __name__)


@dash_bp.route("/")
def home():
    return render_template("dashboard.html")
