#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Short-lived fn-02 MQTT protocol actor for same-site cross-worker E2E."""

import argparse
import base64
import copy
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import ssl
import sys
import tempfile
import threading
import time
import uuid


RELEASE_ROOT = pathlib.Path(__file__).resolve().parents[2]
MASTER_ROOT = RELEASE_ROOT / "master_node" / "master_node"
if str(MASTER_ROOT) not in sys.path:
    sys.path.insert(0, str(MASTER_ROOT))

from src.library._generic_system_contract_library import (
    CONFIG_NORM_VERSION,
    OPERATION_FORMAT,
    TYPE_SCHEMA_VERSION,
    apply_operations,
    authority_flow_contracts,
    canonical_bytes,
    diff_configs,
    fingerprint,
    normalize_legacy_ingress,
    persistence_contracts,
    shard_library,
    type_library,
    validate_scope_operations,
)
from src.library._generic_system_contract_library.protocol_contracts import (
    CONFIG_PROPOSE,
    CONTRACTS_VERSION,
    CROSS_FULL,
    CROSS_PATCH,
    MIRROR_REQUEST,
    MIRROR_SNAPSHOT,
    NODE_FULL,
    NODE_PATCH,
    PROTOCOL_V1,
    PROTOCOL_V2,
    validate_full_payload,
)


VERSION = "0.25.0-e2e.3"
OUTER_PROTOCOL = "automation.mqtt.v1"
EXPECTED_IDENTITY = "urn:automation:site-main:worker:fn-02"
EXPECTED_NODE_ID = "fn-02"
SUPPORTED_COMMANDS = (
    CONFIG_PROPOSE,
    MIRROR_REQUEST,
    NODE_FULL,
    NODE_PATCH,
    CROSS_FULL,
    CROSS_PATCH,
    "distributed.actuator.execute",
)
STATUS_CODES = {"PASSED": 0, "FAILED": 1, "BLOCKED": 3, "SKIPPED": 4}


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_json(path, label):
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("BLOCKED: invalid {}: {}".format(label, exc)) from exc
    if not isinstance(value, dict):
        raise RuntimeError("BLOCKED: {} must contain a JSON object".format(label))
    return value


def _write_json(path, value, **kwargs):
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL if kwargs.get("exclusive", False) else os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        os.write(descriptor, payload.encode("utf-8") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _absolute_file(value, label, base):
    path = pathlib.Path(str(value or "")).expanduser()
    if not path.is_absolute():
        path = pathlib.Path(base) / path
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError("BLOCKED: {} is unavailable: {}".format(label, path))
    return path


def _validate_config(config):
    required = (
        "broker_host", "broker_port", "ca_file", "cert_file", "key_file",
        "client_id", "site_id", "node_id", "master_node_id",
    )
    missing = [name for name in required if config.get(name) in (None, "")]
    if missing:
        raise RuntimeError("BLOCKED: fn-02 config misses " + ", ".join(missing))
    if config["site_id"] != "site-main" or config["node_id"] != EXPECTED_NODE_ID:
        raise RuntimeError("BLOCKED: fn-02 simulator is fixed to site-main/fn-02")
    port = int(config["broker_port"])
    if not 1 <= port <= 65535:
        raise RuntimeError("BLOCKED: MQTT broker port is invalid")
    heartbeat = float(config.get("heartbeat_interval_s") or 10.0)
    if not 1.0 <= heartbeat <= 30.0:
        raise RuntimeError("BLOCKED: heartbeat interval must be between 1 and 30 seconds")
    maximum = int(config.get("max_packet_size") or 1024 * 1024)
    if not 65536 <= maximum <= 4 * 1024 * 1024:
        raise RuntimeError("BLOCKED: max_packet_size violates distributed hard limits")
    context = {
        "config": copy.deepcopy(config),
        "broker_host": str(config["broker_host"]),
        "broker_port": port,
        "ca_file": _absolute_file(config["ca_file"], "MQTT CA", config["_config_dir"]),
        "cert_file": _absolute_file(
            config["cert_file"], "fn-02 certificate", config["_config_dir"]
        ),
        "key_file": _absolute_file(
            config["key_file"], "fn-02 private key", config["_config_dir"]
        ),
        "client_id": str(config["client_id"]),
        "site_id": "site-main",
        "node_id": EXPECTED_NODE_ID,
        "master_node_id": str(config["master_node_id"]),
        "heartbeat_interval_s": heartbeat,
        "max_packet_size": maximum,
        "lock_file": pathlib.Path(
            str(config.get("lock_file") or "/tmp/automation-e2e-site-main-fn-02.lock")
        ).expanduser().resolve(),
    }
    if not context["client_id"].startswith("automation-e2e-"):
        raise RuntimeError("BLOCKED: fn-02 client_id must use the dedicated E2E prefix")
    if context["broker_host"] != str(config.get("server_hostname") or context["broker_host"]):
        raise RuntimeError("BLOCKED: paho hostname verification requires broker_host=server_hostname")
    if (context["key_file"].stat().st_mode & 0o077) != 0:
        raise RuntimeError("BLOCKED: fn-02 private key permissions must be 0600 or stricter")
    return context


def _validate_certificate(context):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509.oid import ExtendedKeyUsageOID
    except ImportError as exc:
        raise RuntimeError("BLOCKED: cryptography from requirements-e2e.txt is unavailable") from exc
    certificate = x509.load_pem_x509_certificate(context["cert_file"].read_bytes())
    private_key = serialization.load_pem_private_key(
        context["key_file"].read_bytes(), password=None
    )
    public_cert = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if public_cert != public_key:
        raise RuntimeError("BLOCKED: fn-02 certificate and private key differ")
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        identities = san.get_values_for_type(x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound as exc:
        raise RuntimeError("BLOCKED: fn-02 certificate has no URI SAN") from exc
    if identities != [EXPECTED_IDENTITY]:
        raise RuntimeError("BLOCKED: fn-02 certificate URI identity differs")
    try:
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound as exc:
        raise RuntimeError("BLOCKED: fn-02 certificate has no clientAuth EKU") from exc
    if ExtendedKeyUsageOID.CLIENT_AUTH not in usage:
        raise RuntimeError("BLOCKED: fn-02 certificate is not a client certificate")
    now = datetime.datetime.now(datetime.timezone.utc)
    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        raise RuntimeError("BLOCKED: fn-02 certificate is outside its validity period")
    return {
        "certificate_identity": EXPECTED_IDENTITY,
        "serial_number": str(certificate.serial_number),
    }


def _proof_time(proof):
    if proof.get("captured_at_epoch_s") is not None:
        return float(proof["captured_at_epoch_s"])
    text = str(proof.get("captured_at") or "").replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise RuntimeError("BLOCKED: broker proof timestamp is invalid") from exc


def _verify_broker_proof_signature(context, proof):
    if proof.get("signature_algorithm") != "ed25519":
        raise RuntimeError("BLOCKED: Broker proof signature algorithm differs")
    encoded = str(proof.get("signature") or "")
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("BLOCKED: Broker proof signature is invalid Base64") from exc
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public_bytes = context["broker_proof_public_key"].read_bytes()
        public_key = serialization.load_pem_public_key(public_bytes)
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("not an Ed25519 key")
        signed = copy.deepcopy(proof)
        signed.pop("signature", None)
        payload = json.dumps(
            signed, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        public_key.verify(signature, payload)
    except Exception as exc:
        raise RuntimeError("BLOCKED: Broker proof signature verification failed") from exc
    signer_sha256 = hashlib.sha256(public_bytes).hexdigest()
    expected = str(context.get("broker_proof_key_sha256") or "")
    if expected and signer_sha256 != expected:
        raise RuntimeError("BLOCKED: Broker proof public key differs from the pinned authority")
    return signer_sha256


def _validate_broker_proof(context, proof):
    signer_sha256 = _verify_broker_proof_signature(context, proof)
    required_false = ("active_client_id", "active_certificate_identity")
    if proof.get("authoritative") is not True:
        raise RuntimeError("BLOCKED: Broker session proof is not authoritative")
    if proof.get("proof_kind") != "broker-session-reservation":
        raise RuntimeError("BLOCKED: Broker proof is not an exclusive session reservation")
    if str(proof.get("site_id") or "") != context["site_id"]:
        raise RuntimeError("BLOCKED: Broker proof site differs")
    if str(proof.get("node_id") or "") != context["node_id"]:
        raise RuntimeError("BLOCKED: Broker proof node differs")
    if str(proof.get("client_id") or "") != context["client_id"]:
        raise RuntimeError("BLOCKED: Broker proof client_id differs")
    if str(proof.get("certificate_identity") or "") != EXPECTED_IDENTITY:
        raise RuntimeError("BLOCKED: Broker proof certificate identity differs")
    if any(proof.get(name) is not False for name in required_false):
        raise RuntimeError("BLOCKED: an fn-02 client or certificate session is already active")
    if proof.get("reservation_active") is not True or str(
            proof.get("reservation_run_id") or "") != context["run_id"]:
        raise RuntimeError("BLOCKED: Broker reservation is not owned by this E2E run")
    reservation_id = str(proof.get("reservation_id") or "")
    if not reservation_id:
        raise RuntimeError("BLOCKED: Broker reservation has no stable identity")
    reservation_expires = str(proof.get("reservation_expires_at") or "").replace(
        "Z", "+00:00"
    )
    try:
        reservation_deadline = datetime.datetime.fromisoformat(
            reservation_expires
        ).timestamp()
    except ValueError as exc:
        raise RuntimeError("BLOCKED: Broker reservation expiry is invalid") from exc
    minimum_deadline = time.time() + float(context.get("duration_s") or 900.0) + 60.0
    if reservation_deadline < minimum_deadline:
        raise RuntimeError("BLOCKED: Broker reservation does not cover the simulator runtime")
    if any(proof.get(name) is not True for name in (
            "mtls_identity_verified", "foreign_site_publish_denied",
            "foreign_site_subscribe_denied")):
        raise RuntimeError("BLOCKED: Broker proof lacks mTLS foreign-site ACL probes")
    maximum_age = min(60.0, float(context["config"].get("proof_max_age_s") or 30.0))
    age = time.time() - _proof_time(proof)
    if age < -5.0 or age > maximum_age:
        raise RuntimeError("BLOCKED: Broker session proof is stale")
    source = str(proof.get("source") or "")
    if not source:
        raise RuntimeError("BLOCKED: Broker proof does not name its management source")
    return {
        "source": source,
        "age_s": round(age, 3),
        "authoritative": True,
        "reservation_id": reservation_id,
        "reservation_run_id": context["run_id"],
        "reservation_source": source,
        "reservation_expires_at": proof.get("reservation_expires_at"),
        "signer_sha256": signer_sha256,
        "foreign_site_publish_denied": True,
        "foreign_site_subscribe_denied": True,
        "mtls_identity_verified": True,
    }


def _load_exclusive_broker_proof(context):
    source = _load_json(context["broker_proof_path"], "Broker session proof")
    proof = _validate_broker_proof(context, source)
    context["broker_proof"] = copy.deepcopy(source)
    context["broker_proof_result"] = copy.deepcopy(proof)
    context["broker_proof_valid"] = True
    return proof


def _acquire_lock(context):
    context["lock_file"].parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(context["lock_file"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise RuntimeError("BLOCKED: another local fn-02 simulator holds the lock") from exc
    os.ftruncate(descriptor, 0)
    os.write(descriptor, (context["run_id"] + "\n").encode("utf-8"))
    context["lock_descriptor"] = descriptor
    return str(context["lock_file"])


def _release_lock(context):
    descriptor = context.get("lock_descriptor")
    if descriptor is None:
        return True
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)
    context["lock_descriptor"] = None
    return True


def _topics(context):
    base = "automation/v1/{site}".format(site=context["site_id"])
    node = base + "/nodes/" + context["node_id"]
    master = base + "/nodes/" + context["master_node_id"]
    return {
        "node_commands": node + "/commands/#",
        "broadcast_commands": base + "/broadcast/commands/#",
        "node_acks": node + "/acks/#",
        "status": node + "/status",
        "heartbeat": node + "/heartbeat",
        "registration": node + "/registration",
        "config_state": node + "/config/state",
        "master_commands": master + "/commands",
        "master_acks": master + "/acks",
    }


def _base_envelope(context, kind):
    return {
        "protocol": OUTER_PROTOCOL,
        "kind": kind,
        "message_id": str(uuid.uuid4()),
        "site_id": context["site_id"],
        "source_node": context["node_id"],
        "created_at_s": time.time(),
    }


def _registration_capabilities():
    return {
        OPERATION_FORMAT: True,
        "authority_epoch_v1": True,
        "authority_transfer_v1": False,
        "canonical_config_v9": True,
        "chunk_reassembly": False,
        "config_proposals": True,
        "cross_fact_consumer": True,
        "cross_full": True,
        "cross_owner_accept_v1": True,
        "cross_patch": True,
        "cross_runtime": True,
        "direct_proposal_mtls": False,
        "local_automation_authority": True,
        "master_automation_execution": False,
        "mirror_v1": True,
        "node_full": True,
        "node_patch": True,
        "remote_actuator": True,
        "restore_quarantine_v1": False,
    }


def _registration_payload(context):
    state = context["state"]
    node_replica = copy.deepcopy(state["node_replica"])
    node_config = state.get("node_replica_config") or {}
    controller_uuids = sorted({
        str(controller.get("controller_uuid") or "")
        for controller in node_config.get("controllers") or ()
        if isinstance(controller, dict) and controller.get("controller_uuid")
    })
    cross_replica = copy.deepcopy(state["cross_replica"])
    if int(cross_replica.get("revision") or 0) <= 0:
        cross_replica = None
    return {
        "node_id": context["node_id"],
        "site_id": context["site_id"],
        "state": state["reported_state"],
        "software_version": VERSION,
        "protocol": PROTOCOL_V2,
        "protocol_version": PROTOCOL_V2,
        "protocol_versions": [PROTOCOL_V1, PROTOCOL_V2],
        "contracts_version": CONTRACTS_VERSION,
        "type_schema_version": TYPE_SCHEMA_VERSION,
        "config_norm_version": copy.deepcopy(CONFIG_NORM_VERSION),
        "config_authority": "node_local",
        "authority_epoch": int(node_replica.get("authority_epoch") or 0),
        "observed_global_revision": state["observed_global_revision"],
        "authority_revision": state["observed_global_revision"],
        "live_config_revision": int(state.get("local_config_revision") or 0),
        "local_config_revision": int(state.get("local_config_revision") or 0),
        "pending_proposal_count": 0,
        "fingerprint": node_replica.get("fingerprint"),
        "coordinator_term": state["coordinator_term"],
        "runtime_nonce": context["run_id"],
        "node_replica": node_replica,
        "cross_replica": cross_replica,
        "controller_uuids": controller_uuids,
        "capabilities": _registration_capabilities(),
        "max_packet_size": context["max_packet_size"],
        "health": {
            "simulation": True,
            "e2e_run_id": context["run_id"],
            "command_queue_idle": state.get("command_active") is not True,
            "drain_sequence": int(state.get("drain_sequence") or 0),
            "last_command": state.get("last_command"),
            "last_command_message_id": state.get("last_command_message_id"),
            "last_operation_id": state.get("last_operation_id"),
        },
    }
def _publish(context, topic, envelope, **kwargs):
    encoded = canonical_bytes(envelope)
    if len(encoded) > context["max_packet_size"]:
        raise RuntimeError("FAILED: simulator envelope exceeds max_packet_size")
    info = context["client"].publish(
        topic, encoded, qos=int(kwargs.get("qos", 1)), retain=bool(kwargs.get("retain", False))
    )
    if int(info.rc) != 0:
        raise RuntimeError("FAILED: MQTT publish was rejected with rc={}".format(info.rc))
    return True


def _publish_status(context, state):
    envelope = _base_envelope(context, "status")
    envelope.update({"state": state, "graceful": state == "offline", "simulation": True})
    return _publish(context, context["topics"]["status"], envelope, qos=1, retain=True)


def _publish_identity_heartbeat(context):
    envelope = _base_envelope(context, "heartbeat")
    envelope["payload"] = _registration_payload(context)
    return _publish(context, context["topics"]["heartbeat"], envelope, qos=0, retain=False)


def _publish_registration_marker(context):
    envelope = _base_envelope(context, "node_registration")
    envelope["payload"] = _registration_payload(context)
    return _publish(context, context["topics"]["registration"], envelope, qos=1, retain=True)


def _publish_master_command(context, command, payload):
    envelope = _base_envelope(context, "command")
    envelope.update({
        "target_node": context["master_node_id"],
        "expires_at_s": time.time() + 60.0,
        "principal": context["node_id"],
        "command": command,
        "payload": copy.deepcopy(payload),
    })
    topic = context["topics"]["master_commands"] + "/" + command
    _publish(context, topic, envelope, qos=1, retain=False)
    return envelope["message_id"]


def _publish_registration(context):
    payload = _registration_payload(context)
    _publish_registration_marker(context)
    return _publish_master_command(context, "distributed.node.register", payload)


def _publish_heartbeat(context):
    context["state"]["drain_sequence"] = int(
        context["state"].get("drain_sequence") or 0
    ) + 1
    payload = _registration_payload(context)
    _publish_identity_heartbeat(context)
    return _publish_master_command(context, "distributed.node.heartbeat", payload)


def _ack_envelope(context, command, result):
    envelope = _base_envelope(context, "ack")
    envelope.update({
        "message_id": str(command.get("message_id") or ""),
        "target_node": str(command.get("source_node") or context["master_node_id"]),
        "command": str(command.get("command") or ""),
        "ok": bool(result.get("ok")),
        "result": copy.deepcopy(result),
    })
    return envelope


def _publish_ack(context, command, result):
    target = str(command.get("source_node") or context["master_node_id"])
    base = "automation/v1/{}/nodes/{}/acks".format(context["site_id"], target)
    topic = base + "/" + str(command.get("command") or "unknown")
    return _publish(context, topic, _ack_envelope(context, command, result), qos=1)


def _command_error(code, message):
    return {"ok": False, "code": code, "message": message, "simulation": True}


def _validate_command(context, command):
    if not isinstance(command, dict) or command.get("protocol") != OUTER_PROTOCOL:
        return _command_error("invalid_envelope", "outer MQTT envelope differs")
    if command.get("kind") != "command":
        return _command_error("invalid_envelope", "message is not a command")
    if command.get("site_id") != context["site_id"]:
        return _command_error("identity_mismatch", "command belongs to another site")
    target = str(command.get("target_node") or "")
    if target != context["node_id"]:
        return _command_error("identity_mismatch", "command targets another node")
    if str(command.get("source_node") or "") != context["master_node_id"]:
        return _command_error("identity_mismatch", "command source is not the Master")
    if float(command.get("expires_at_s") or 0.0) <= time.time():
        return _command_error("expired", "command TTL has expired")
    name = str(command.get("command") or "")
    if name not in SUPPORTED_COMMANDS:
        return _command_error("unknown_command", "command is outside simulator allowlist")
    payload = command.get("payload")
    if not isinstance(payload, dict):
        return _command_error("invalid_payload", "command payload must be an object")
    protocol = payload.get("protocol")
    if protocol is not None and protocol not in (PROTOCOL_V1, PROTOCOL_V2):
        return _command_error("unsupported_protocol", "distributed protocol differs")
    if name == CONFIG_PROPOSE and protocol != PROTOCOL_V2:
        return _command_error("unsupported_protocol", "canonical proposal requires v2")
    return None
def _replica_name(command):
    return "cross_replica" if command in (CROSS_FULL, CROSS_PATCH) else "node_replica"


def _validate_rollout_fence(context, payload, name):
    term = int(payload.get("coordinator_term") or 0)
    global_revision = int(
        payload.get("global_revision") or payload.get("authority_revision") or 0
    )
    target_revision = int(payload.get("target_scope_revision") or 0)
    epoch = int(payload.get("execution_epoch") or 0)
    replica = context["state"][name]
    if term <= 0 or term < int(context["state"]["coordinator_term"]):
        return _command_error("stale_authority_term", "rollout authority term is stale")
    if global_revision <= 0 or global_revision < int(
            context["state"]["observed_global_revision"]):
        return _command_error("stale_revision", "rollout global revision is stale")
    if target_revision <= 0 or target_revision < int(replica.get("revision") or 0):
        return _command_error("stale_revision", "rollout scope revision is stale")
    if (
        target_revision == int(replica.get("revision") or 0)
        and target_revision > 0
        and str(payload.get("target_scope_fingerprint") or "")
        != str(replica.get("fingerprint") or "")
    ):
        return _command_error("conflict", "rollout reused a scope revision with another hash")
    if epoch <= 0 or epoch < int(replica.get("execution_epoch") or 0):
        return _command_error("stale_execution_epoch", "rollout execution epoch is stale")
    current_scope = str(replica.get("scope") or "")
    supplied_scope = str(payload.get("scope_id") or "")
    if current_scope and int(replica.get("revision") or 0) > 0 and supplied_scope != current_scope:
        return _command_error("identity_mismatch", "rollout scope changed unexpectedly")
    return None


def _node_scope_head(context):
    replica = context["state"]["node_replica"]
    return authority_flow_contracts.make_scope_head(
        replica["scope"],
        authority_mode="node_local",
        authority_epoch=replica.get("authority_epoch", 0),
        scope_revision=replica.get("revision", 0),
        execution_epoch=replica.get("execution_epoch", 0),
        fingerprint=replica.get("fingerprint"),
        locked=False,
        node_authoritative_committed=True,
    )


def _proposal_from_command(context, command):
    payload = copy.deepcopy(command.get("payload") or {})
    baseline = context["state"].get("node_replica_config")
    if not isinstance(baseline, dict):
        raise ValueError("authoritative Node baseline is unavailable")
    name = str(command.get("command") or "")
    if name == NODE_FULL:
        operations = normalize_legacy_ingress(
            baseline, {"config": payload.get("config")})
    elif name == NODE_PATCH:
        operations = validate_scope_operations(
            payload.get("operations"),
            operation_format=payload.get("operation_format", OPERATION_FORMAT),
        )
    else:
        operations = validate_scope_operations(
            payload.get("operations"),
            operation_format=payload.get("operation_format"),
        )
    replica = context["state"]["node_replica"]
    base_authority_epoch = (
        payload["base_authority_epoch"]
        if payload.get("base_authority_epoch") is not None
        else replica.get("authority_epoch", 0)
    )
    base_scope_revision = (
        payload["base_scope_revision"]
        if payload.get("base_scope_revision") is not None
        else replica.get("revision", 0)
    )
    return authority_flow_contracts.make_proposal(
        payload.get("scope") or payload.get("scope_id") or replica["scope"],
        operations,
        proposal_id=payload.get("proposal_id") or command.get("message_id"),
        operation_id=payload.get("operation_id") or command.get("message_id"),
        operation_format=OPERATION_FORMAT,
        base_authority_epoch=base_authority_epoch,
        base_scope_revision=base_scope_revision,
        issued_at_s=payload.get("issued_at_s", command.get("created_at_s", time.time())),
        expires_at_s=payload.get("expires_at_s", command.get("expires_at_s")),
        relay_identity=payload.get("relay_identity"),
        metadata=payload.get("metadata"),
    )


def _validated_node_candidate(context, proposal):
    baseline = context["state"].get("node_replica_config")
    candidate = apply_operations(baseline, proposal["operations"])
    report = type_library.inspect_config_contract(candidate, representation="auto")
    if report.get("status") != "OK":
        return _command_error("invalid_payload", type_library.format_report(report))
    meta_rows = candidate.get("meta") if isinstance(candidate, dict) else None
    meta = meta_rows[0] if isinstance(meta_rows, list) and meta_rows else {}
    if (
        not isinstance(meta, dict)
        or int(meta.get("version") or 0) != 9
        or str(meta.get("scope") or "") != "node_local"
        or str(meta.get("node_id") or "") != context["node_id"]
    ):
        return _command_error(
            "path_denied", "proposal candidate is not the local canonical V9 Node shard")
    return {"ok": True, "config": candidate, "fingerprint": fingerprint(candidate)}


def _mirror_snapshot_payload(context):
    replica = context["state"]["node_replica"]
    config = context["state"].get("node_replica_config")
    snapshot = persistence_contracts.make_scope_snapshot(
        config,
        scope=replica["scope"],
        authority_epoch=replica["authority_epoch"],
        scope_revision=replica["revision"],
        execution_epoch=replica["execution_epoch"],
        fingerprint=replica["fingerprint"],
    )
    mirror_id = "fn02:{}:{}:{}".format(
        replica["authority_epoch"], replica["revision"], replica["fingerprint"][:16])
    return {
        "mirror_id": mirror_id,
        "scope": replica["scope"],
        "authority_epoch": replica["authority_epoch"],
        "scope_revision": replica["revision"],
        "execution_epoch": replica["execution_epoch"],
        "fingerprint": replica["fingerprint"],
        "contracts_version": CONTRACTS_VERSION,
        "type_schema_version": TYPE_SCHEMA_VERSION,
        "config_norm_version": copy.deepcopy(CONFIG_NORM_VERSION),
        "snapshot": snapshot,
    }


def _publish_mirror_snapshot(context):
    payload = _mirror_snapshot_payload(context)
    context["state"]["last_mirror_payload"] = copy.deepcopy(payload)
    context["statistics"]["mirror_snapshots"] += 1
    if context.get("self_test"):
        return {"message_id": None, "payload": payload}
    message_id = _publish_master_command(context, MIRROR_SNAPSHOT, payload)
    return {"message_id": message_id, "payload": payload}


def _handle_mirror_request(context, command):
    payload = command.get("payload") or {}
    replica = context["state"]["node_replica"]
    scope = str(payload.get("scope") or replica["scope"])
    if scope != replica["scope"]:
        return _command_error("path_denied", "Mirror request targets another scope")
    requested_epoch = int(payload.get("authority_epoch") or 0)
    requested_revision = int(payload.get("scope_revision") or 0)
    if requested_epoch > int(replica["authority_epoch"]):
        return _command_error(
            "authority_epoch_stale", "Master Mirror head is newer than local authority")
    if (
        requested_epoch == int(replica["authority_epoch"])
        and requested_revision > int(replica["revision"])
    ):
        return _command_error("stale_revision", "Master Mirror revision is newer than local")
    context["statistics"]["mirror_requests"] += 1
    staged = _publish_mirror_snapshot(context)
    return {
        "ok": True,
        "status": "mirror_snapshot_staged",
        "request_queued": True,
        "mirror_id": staged["payload"]["mirror_id"],
        "scope": replica["scope"],
        "authority_epoch": replica["authority_epoch"],
        "scope_revision": replica["revision"],
        "execution_epoch": replica["execution_epoch"],
        "fingerprint": replica["fingerprint"],
        "simulation": True,
    }


def _apply_node_local_proposal(context, command):
    try:
        proposal = _proposal_from_command(context, command)
    except (KeyError, TypeError, ValueError) as exc:
        code = (
            "unsupported_operation_format"
            if "operation" in str(exc).lower()
            else "invalid_proposal"
        )
        return _command_error(code, str(exc))
    expected_scope = "node:" + context["node_id"]
    if proposal["scope"] != expected_scope:
        return _command_error("path_denied", "proposal targets another scope")
    checked = authority_flow_contracts.validate_proposal(proposal)
    if not checked.get("ok"):
        error = checked.get("error") if isinstance(checked.get("error"), dict) else {}
        return _command_error(
            checked.get("code") or error.get("code") or "invalid_proposal",
            checked.get("message") or error.get("message") or "proposal is invalid",
        )
    fence = authority_flow_contracts.check_proposal_fence(
        proposal, _node_scope_head(context))
    if not fence.get("ok"):
        error = fence.get("error") if isinstance(fence.get("error"), dict) else {}
        return _command_error(
            fence.get("code") or error.get("code") or "stale_revision",
            fence.get("message") or error.get("message") or "proposal fence differs",
        )
    candidate = _validated_node_candidate(context, proposal)
    if not candidate.get("ok"):
        return candidate
    replica = context["state"]["node_replica"]
    next_revision = int(replica["revision"]) + 1
    context["state"]["node_replica_config"] = candidate["config"]
    context["state"]["node_replica"] = {
        "scope": expected_scope,
        "authority_epoch": int(replica["authority_epoch"]),
        "revision": next_revision,
        "fingerprint": candidate["fingerprint"],
        "execution_epoch": int(replica["execution_epoch"]),
    }
    context["state"]["local_config_revision"] = int(
        context["state"].get("local_config_revision") or 0) + 1
    context["statistics"]["proposals_accepted"] += 1
    mirror = _publish_mirror_snapshot(context)
    return {
        "ok": True,
        "code": "accepted",
        "status": "accepted",
        "proposal_id": proposal["proposal_id"],
        "operation_id": proposal["operation_id"],
        "principal": "master_relay",
        "authority_epoch": int(replica["authority_epoch"]),
        "scope_revision": next_revision,
        "fingerprint": candidate["fingerprint"],
        "mirror_id": mirror["payload"]["mirror_id"],
        "simulation": True,
    }

def _apply_full(context, command):
    payload = command["payload"]
    name = _replica_name(command["command"])
    fence = _validate_rollout_fence(context, payload, name)
    if fence:
        return fence
    scope_kind = "cross" if command["command"] == CROSS_FULL else "node_local"
    checked = validate_full_payload(payload, scope_kind=scope_kind)
    if not checked.get("ok"):
        return _command_error(checked.get("code"), checked.get("message"))
    config = copy.deepcopy(payload["config"])
    expected = str(payload.get("target_scope_fingerprint") or "")
    actual = fingerprint(config)
    if not expected or actual != expected:
        return _command_error("fingerprint_mismatch", "Full scope fingerprint differs")
    context["state"][name] = {
        "scope": str(payload.get("scope_id") or ""),
        "revision": int(payload.get("target_scope_revision") or 0),
        "fingerprint": actual,
        "execution_epoch": int(payload.get("execution_epoch") or 0),
    }
    context["state"][name + "_config"] = config
    context["state"]["observed_global_revision"] = int(
        payload.get("global_revision") or payload.get("authority_revision") or 0
    )
    context["state"]["coordinator_term"] = max(
        context["state"]["coordinator_term"], int(payload.get("coordinator_term") or 0)
    )
    context["state"]["execution_epoch"] = max(
        context["state"]["execution_epoch"], int(payload.get("execution_epoch") or 0)
    )
    return {
        "ok": True,
        "status": "applied",
        "scope": context["state"][name]["scope"],
        "scope_revision": context["state"][name]["revision"],
        "fingerprint": actual,
        "coordinator_term": context["state"]["coordinator_term"],
        "simulation": True,
    }


def _apply_patch(context, command):
    payload = command["payload"]
    name = _replica_name(command["command"])
    fence = _validate_rollout_fence(context, payload, name)
    if fence:
        return fence
    replica = context["state"][name]
    baseline = context["state"].get(name + "_config")
    if not isinstance(baseline, dict):
        return _command_error("journal_gap", "Patch arrived before a Full scope")
    if int(payload.get("base_scope_revision") or -1) != int(replica["revision"]):
        return _command_error("patch_base_mismatch", "Patch base revision differs")
    if str(payload.get("base_scope_fingerprint") or "") != str(replica["fingerprint"]):
        return _command_error("patch_base_fingerprint", "Patch base fingerprint differs")
    try:
        updated = apply_operations(baseline, payload.get("operations") or ())
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        return _command_error("patch_validation", str(exc))
    actual = fingerprint(updated)
    expected = str(payload.get("target_scope_fingerprint") or "")
    if actual != expected:
        return _command_error("value_hash_mismatch", "Patch target fingerprint differs")
    context["state"][name + "_config"] = updated
    context["state"][name] = {
        "scope": str(payload.get("scope_id") or replica["scope"]),
        "revision": int(payload.get("target_scope_revision") or 0),
        "fingerprint": actual,
        "execution_epoch": int(payload.get("execution_epoch") or 0),
    }
    context["state"]["observed_global_revision"] = int(
        payload.get("global_revision") or payload.get("authority_revision") or 0
    )
    context["state"]["coordinator_term"] = max(
        context["state"]["coordinator_term"], int(payload.get("coordinator_term") or 0)
    )
    context["state"]["execution_epoch"] = max(
        context["state"]["execution_epoch"], int(payload.get("execution_epoch") or 0)
    )
    return {
        "ok": True,
        "status": "applied",
        "scope": context["state"][name]["scope"],
        "scope_revision": context["state"][name]["revision"],
        "fingerprint": actual,
        "coordinator_term": context["state"]["coordinator_term"],
        "simulation": True,
    }


def _actuator_result(context, command):
    payload = command["payload"]
    term = int(payload.get("coordinator_term") or 0)
    revision = int(payload.get("authority_revision") or 0)
    replica = context["state"]["node_replica"]
    target_revision = int(payload.get("target_node_scope_revision") or 0)
    target_fingerprint = str(payload.get("target_node_scope_fingerprint") or "")
    target_epoch = int(payload.get("target_node_execution_epoch") or 0)
    if term <= 0 or term < context["state"]["coordinator_term"]:
        return _command_error("stale_authority_term", "actuator term is stale")
    if revision <= 0 or revision < int(context["state"]["observed_global_revision"]):
        return _command_error("stale_revision", "actuator authority revision is stale")
    if target_revision != int(replica.get("revision") or 0):
        return _command_error("stale_revision", "actuator Node-scope revision differs")
    if not target_fingerprint or target_fingerprint != str(replica.get("fingerprint") or ""):
        return _command_error("fingerprint_mismatch", "actuator Node-scope fingerprint differs")
    if target_epoch != int(replica.get("execution_epoch") or 0):
        return _command_error("stale_execution_epoch", "actuator Node execution epoch differs")
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    expected = context["fixture"]["target"]
    cross = context["fixture"]["cross"]
    cross_scope = str(task.get("execution_scope") or "")
    cross_epoch = int(task.get("execution_epoch") or 0)
    if cross_scope != str(cross.get("scope") or ""):
        return _command_error("stale_execution_scope", "Cross execution scope differs")
    if cross_epoch <= 0:
        return _command_error("stale_execution_epoch", "Cross execution epoch is absent")
    if str(task.get("controller_uuid") or "") != expected["controller_uuid"]:
        return _command_error("identity_mismatch", "target controller differs")
    if str(task.get("actuator_id") or "") != str(expected["actuator_id"]):
        return _command_error("identity_mismatch", "target actuator differs")
    operation_id = str(payload.get("operation_id") or "")
    if not operation_id:
        return _command_error("invalid_payload", "operation_id is required")
    value = copy.deepcopy(task.get("control_value", task.get("value")))
    context["state"]["actuators"][str(expected["actuator_id"])] = value
    context["actuator_trace"].append({
        "monotonic_ns": time.monotonic_ns(),
        "message_id": str(command.get("message_id") or ""),
        "operation_id": operation_id,
        "controller_uuid": expected["controller_uuid"],
        "actuator_id": expected["actuator_id"],
        "command_value": copy.deepcopy(value),
        "coordinator_term": term,
        "authority_revision": revision,
        "cross_execution_scope": cross_scope,
        "cross_execution_epoch": cross_epoch,
        "target_execution_scope": replica.get("scope"),
        "target_execution_epoch": target_epoch,
        "target_scope_revision": target_revision,
        "target_scope_fingerprint": target_fingerprint,
    })
    result = {
        "success": True,
        "write_succeeded": True,
        "readback_checked": True,
        "readback_verified": True,
        "readback": copy.deepcopy(value),
        "command_value": copy.deepcopy(value),
        "controller_uuid": expected["controller_uuid"],
        "actuator_id": expected["actuator_id"],
        "address": expected["address"],
        "function_type": expected["function_type"],
        "simulation": True,
        "e2e_run_id": context["run_id"],
    }
    terminal = {
        "operation_id": operation_id,
        "coordinator_term": term,
        "authority_revision": revision,
        "execution_scope": replica.get("scope"),
        "execution_epoch": target_epoch,
        "terminal_state": "CONFIRMED",
        "result": result,
        "simulation": True,
    }
    result_message_id = None
    if not context.get("self_test"):
        result_message_id = _publish_master_command(
            context, "distributed.actuator.result", terminal
        )
    context["actuator_trace"][-1]["result_message_id"] = result_message_id
    context["statistics"]["actuator_results"] += 1
    context["state"]["coordinator_term"] = max(
        context["state"]["coordinator_term"], term
    )
    return {
        "ok": True,
        "state": "DISPATCHED",
        "operation_id": operation_id,
        "coordinator_term": term,
        "simulation": True,
    }


def _execute_command(context, command):
    error = _validate_command(context, command)
    if error:
        return error
    message_id = str(command.get("message_id") or "")
    cached = context["dedupe"].get(message_id)
    if cached is not None:
        context["statistics"]["duplicates"] += 1
        return copy.deepcopy(cached)
    name = command["command"]
    if name == MIRROR_REQUEST:
        result = _handle_mirror_request(context, command)
    elif name in (CONFIG_PROPOSE, NODE_FULL, NODE_PATCH):
        result = _apply_node_local_proposal(context, command)
    elif name == CROSS_FULL:
        result = _apply_full(context, command)
    elif name == CROSS_PATCH:
        result = _apply_patch(context, command)
    else:
        result = _actuator_result(context, command)
    context["dedupe"][message_id] = copy.deepcopy(result)
    while len(context["dedupe"]) > 2048:
        context["dedupe"].pop(next(iter(context["dedupe"])))
    context["statistics"]["commands"] += 1
    if result.get("ok") and name in (NODE_FULL, CROSS_FULL):
        context["statistics"]["full_applies"] += 1
    if result.get("ok") and name in (NODE_PATCH, CROSS_PATCH):
        context["statistics"]["patch_applies"] += 1
    if result.get("ok") and name in (
        CONFIG_PROPOSE, NODE_FULL, NODE_PATCH, CROSS_FULL, CROSS_PATCH,
    ):
        context["statistics"]["scope_applies"] += 1
    return result


def _on_connect(context, *args):
    client = args[0]
    reason_code = args[3] if len(args) > 3 else 1
    if int(reason_code) != 0:
        context["last_error"] = "MQTT connect rejected: " + str(reason_code)
        context["stop_event"].set()
        return
    client.subscribe(context["topics"]["node_commands"], qos=1)
    client.subscribe(context["topics"]["broadcast_commands"], qos=1)
    client.subscribe(context["topics"]["node_acks"], qos=1)
    context["connected_event"].set()


def _on_disconnect(context, *args):
    if not context["stopping"]:
        reason = args[3] if len(args) > 3 else "unknown"
        context["last_error"] = "unexpected MQTT disconnect: " + str(reason)
        context["stop_event"].set()


def _on_message(context, *args):
    message = args[2]
    if "/acks/" in str(message.topic):
        try:
            envelope = json.loads(bytes(message.payload).decode("utf-8"))
            result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
            desired = result.get("desired_state")
            if desired in ("ACTIVE", "DRAINING", "MAINTENANCE", "QUARANTINED"):
                with context["state_lock"]:
                    context["state"]["reported_state"] = desired
        except Exception as exc:
            context["last_error"] = "Master ACK handling failed: " + str(exc)
            context["stop_event"].set()
        return
    try:
        if getattr(message, "retain", False):
            raise RuntimeError("retained command is forbidden")
        command = json.loads(bytes(message.payload).decode("utf-8"))
        with context["state_lock"]:
            context["state"]["command_active"] = True
            result = _execute_command(context, command)
        _publish_ack(context, command, result)
        with context["state_lock"]:
            context["state"]["command_active"] = False
            context["state"]["last_command"] = str(command.get("command") or "")
            context["state"]["last_command_message_id"] = str(
                command.get("message_id") or ""
            )
            operation_id = str(result.get("operation_id") or "")
            if operation_id:
                context["state"]["last_operation_id"] = operation_id
            _publish_heartbeat(context)
    except Exception as exc:
        context["last_error"] = "command handling failed: " + str(exc)
        context["stop_event"].set()


def _build_client(context):
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise RuntimeError("BLOCKED: paho-mqtt from requirements-e2e.txt is unavailable") from exc
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=context["client_id"],
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )
    client.tls_set(
        ca_certs=str(context["ca_file"]),
        certfile=str(context["cert_file"]),
        keyfile=str(context["key_file"]),
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.tls_insecure_set(False)
    client.max_inflight_messages_set(20)
    client.max_queued_messages_set(100)
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    client.on_connect = _bind_callback(_on_connect, context)
    client.on_disconnect = _bind_callback(_on_disconnect, context)
    client.on_message = _bind_callback(_on_message, context)
    return client


def _bind_callback(function, context):
    def callback(*args):
        return function(context, *args)
    return callback


def _load_node_authority_fixture(node_id):
    authority_config = _load_json(
        RELEASE_ROOT / "master_node" / "master_node" / "_config"
        / "application" / "initial_config_v9.json",
        "canonical V9 E2E seed fixture",
    )
    config = shard_library.build_local_execution_shard(authority_config, node_id)
    report = type_library.inspect_config_contract(config, representation="auto")
    if report.get("status") != "OK":
        raise RuntimeError(
            "BLOCKED: fn-02 E2E seed fixture is invalid: "
            + type_library.format_report(report))
    return config


def _initial_state(node_config):
    if not isinstance(node_config, dict):
        raise ValueError("fn-02 authoritative Node config must be an object")
    node_fingerprint = fingerprint(node_config)
    return {
        "reported_state": "ACTIVE",
        "coordinator_term": 0,
        "execution_epoch": 1,
        "observed_global_revision": 0,
        "local_config_revision": 1,
        "node_replica": {
            "scope": "node:fn-02",
            "authority_epoch": 1,
            "revision": 1,
            "fingerprint": node_fingerprint,
            "execution_epoch": 1,
        },
        "cross_replica": {
            "scope": "cross:fn-02.default",
            "authority_epoch": 0,
            "revision": 0,
            "fingerprint": "",
            "execution_epoch": 0,
        },
        "node_replica_config": copy.deepcopy(node_config),
        "cross_replica_config": None,
        "last_mirror_payload": None,
        "actuators": {},
        "command_active": False,
        "drain_sequence": 0,
        "last_command": None,
        "last_command_message_id": None,
        "last_operation_id": None,
    }
def _site_isolation_probe(context):
    command = {
        "protocol": OUTER_PROTOCOL,
        "kind": "command",
        "message_id": "site-isolation-probe",
        "site_id": "site_other",
        "source_node": context["master_node_id"],
        "target_node": context["node_id"],
        "expires_at_s": time.time() + 30.0,
        "command": NODE_FULL,
        "payload": {"protocol": PROTOCOL_V2},
    }
    error = _validate_command(context, command)
    if not error or error.get("code") != "identity_mismatch":
        raise RuntimeError("FAILED: fn-02 simulator accepted a foreign-site envelope")
    proof = context.get("broker_proof") or {}
    verified = context.get("broker_proof_result") or {}
    if proof.get("foreign_site_publish_denied") is not True \
            or proof.get("foreign_site_subscribe_denied") is not True \
            or proof.get("mtls_identity_verified") is not True \
            or len(str(verified.get("signer_sha256") or "")) != 64:
        raise RuntimeError("BLOCKED: authoritative Broker ACL isolation proof is absent")
    return {
        "foreign_site_rejected": True,
        "foreign_site_publish_denied": True,
        "foreign_site_subscribe_denied": True,
        "mtls_identity_verified": True,
        "broker_authoritative": True,
        "broker_proof_authenticated": True,
        "broker_proof_signer_sha256": verified.get("signer_sha256"),
        "cross_site": False,
        "contract_only": False,
    }


def _signal_stop(context, *_args):
    context["stop_event"].set()


def _report(context, status):
    return {
        "version": VERSION,
        "status": status,
        "run_id": context["run_id"],
        "simulation": True,
        "site_id": context["site_id"],
        "node_id": context["node_id"],
        "cross_site": False,
        "exclusive_lock_acquired": context.get("lock_descriptor") is not None,
        "broker_session_exclusive": context.get("broker_proof_valid", False),
        "broker_reservation_id": (context.get("broker_proof_result") or {}).get(
            "reservation_id"
        ),
        "broker_reservation_source": (context.get("broker_proof_result") or {}).get(
            "reservation_source"
        ),
        "broker_reservation_expires_at": (
            context.get("broker_proof_result") or {}
        ).get("reservation_expires_at"),
        "broker_proof_signer_sha256": (
            context.get("broker_proof_result") or {}
        ).get("signer_sha256"),
        "state": copy.deepcopy(context["state"]),
        "statistics": copy.deepcopy(context["statistics"]),
        "actuator_trace": copy.deepcopy(context.get("actuator_trace") or []),
        "last_error": context.get("last_error"),
        "updated_at": _utc_now(),
    }


def _run(context, duration_s):
    if context.get("lock_descriptor") is None:
        _acquire_lock(context)
    try:
        broker_proof = _load_exclusive_broker_proof(context)
        context["client"] = _build_client(context)
        context["client"].will_set(
            context["topics"]["status"],
            canonical_bytes(dict(
                _base_envelope(context, "status"), state="offline", graceful=False
            )),
            qos=1,
            retain=True,
        )
        signal.signal(signal.SIGINT, _bind_callback(_signal_stop, context))
        signal.signal(signal.SIGTERM, _bind_callback(_signal_stop, context))
        context["client"].connect(context["broker_host"], context["broker_port"], keepalive=30)
        context["client"].loop_start()
        if not context["connected_event"].wait(timeout=15.0):
            raise RuntimeError("FAILED: fn-02 MQTT connection did not become ready")
        _publish_status(context, "online")
        _publish_identity_heartbeat(context)
        time.sleep(0.25)
        _publish_registration(context)
        _publish_heartbeat(context)
        if context.get("ready_file"):
            _write_json(context["ready_file"], {
                "version": VERSION,
                "status": "PASSED",
                "run_id": context["run_id"],
                "node_id": context["node_id"],
                "broker_session_exclusive": True,
                "broker_reservation_id": broker_proof["reservation_id"],
                "broker_reservation_source": broker_proof["reservation_source"],
                "broker_reservation_expires_at": broker_proof["reservation_expires_at"],
                "broker_proof_signer_sha256": broker_proof["signer_sha256"],
                "cross_site": False,
                "ready_at": _utc_now(),
            }, exclusive=True)
        deadline = time.monotonic() + duration_s
        while not context["stop_event"].wait(context["heartbeat_interval_s"]):
            if time.monotonic() >= deadline:
                break
            with context["state_lock"]:
                _publish_heartbeat(context)
        if context["last_error"]:
            raise RuntimeError("FAILED: " + context["last_error"])
        return _report(context, "PASSED")
    finally:
        context["stopping"] = True
        if context.get("client"):
            try:
                if context["connected_event"].is_set():
                    _publish_status(context, "offline")
                context["client"].disconnect()
            except Exception:
                pass
            context["client"].loop_stop()
        _release_lock(context)


def _broker_signature_self_test():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def signed_proof(private_key):
        proof = {
            "proof_kind": "offline-signature-regression",
            "reservation_id": "self-test-reservation",
            "active_client_id": False,
            "signature_algorithm": "ed25519",
        }
        payload = json.dumps(
            proof, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        proof["signature"] = base64.b64encode(private_key.sign(payload)).decode("ascii")
        return proof

    with tempfile.TemporaryDirectory(prefix="fn02-signature-") as directory:
        private_key = Ed25519PrivateKey.generate()
        public_path = pathlib.Path(directory) / "broker-proof-public.pem"
        public_path.write_bytes(private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        signer_sha256 = hashlib.sha256(public_path.read_bytes()).hexdigest()
        context = {
            "broker_proof_public_key": public_path,
            "broker_proof_key_sha256": signer_sha256,
        }
        valid = signed_proof(private_key)
        if len(_verify_broker_proof_signature(context, valid)) != 64:
            raise AssertionError("valid canonical Ed25519 Broker proof was rejected")
        tampered = copy.deepcopy(valid)
        tampered["active_client_id"] = True
        wrong_signer = signed_proof(Ed25519PrivateKey.generate())
        for label, proof in (("tampered", tampered), ("wrong signer", wrong_signer)):
            try:
                _verify_broker_proof_signature(context, proof)
            except RuntimeError:
                continue
            raise AssertionError(label + " Ed25519 Broker proof was accepted")
        wrong_pin = copy.deepcopy(context)
        wrong_pin["broker_proof_key_sha256"] = "0" * 64
        try:
            _verify_broker_proof_signature(wrong_pin, valid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("wrong pinned Broker proof key hash was accepted")


def _self_test(fixture_path):
    fixture = _load_json(fixture_path, "cross fixture")
    config = _load_node_authority_fixture("fn-02")
    context = {
        "site_id": "site-main",
        "node_id": "fn-02",
        "master_node_id": "master-01",
        "max_packet_size": 1024 * 1024,
        "run_id": "self-test",
        "fixture": fixture,
        "self_test": True,
        "state": _initial_state(config),
        "dedupe": {},
        "statistics": {
            "commands": 0,
            "duplicates": 0,
            "scope_applies": 0,
            "full_applies": 0,
            "patch_applies": 0,
            "proposals_accepted": 0,
            "mirror_requests": 0,
            "mirror_snapshots": 0,
            "actuator_results": 0,
        },
        "actuator_trace": [],
    }
    initial = context["state"]["node_replica"]
    mirror_request = {
        "protocol": OUTER_PROTOCOL,
        "kind": "command",
        "message_id": "mirror-request-1",
        "site_id": "site-main",
        "source_node": "master-01",
        "target_node": "fn-02",
        "expires_at_s": time.time() + 60,
        "command": MIRROR_REQUEST,
        "payload": {
            "scope": "node:fn-02",
            "authority_epoch": 0,
            "scope_revision": 0,
            "fingerprint": "",
        },
    }
    mirrored = _execute_command(context, mirror_request)
    mirror_payload = context["state"].get("last_mirror_payload") or {}
    if (
        not mirrored.get("ok")
        or mirror_payload.get("fingerprint") != initial["fingerprint"]
        or (mirror_payload.get("snapshot") or {}).get("config") != config
    ):
        raise AssertionError(
            "Worker-to-Master Mirror self-test failed: "
            + json.dumps(mirrored, ensure_ascii=False, sort_keys=True))

    candidate = copy.deepcopy(config)
    candidate["meta"][0]["description"] = str(
        candidate["meta"][0].get("description") or "") + " E2E proposal."
    operations = list(diff_configs(config, candidate))
    proposal_payload = {
        "protocol": PROTOCOL_V2,
        "contracts_version": CONTRACTS_VERSION,
        "operation": "scope_operations",
        "operation_format": OPERATION_FORMAT,
        "scope": "node:fn-02",
        "proposal_id": "proposal-1",
        "operation_id": "operation-1",
        "base_authority_epoch": initial["authority_epoch"],
        "base_scope_revision": initial["revision"],
        "operations": operations,
        "issued_at_s": time.time(),
        "expires_at_s": time.time() + 60,
    }
    command = {
        "protocol": OUTER_PROTOCOL,
        "kind": "command",
        "message_id": "proposal-command-1",
        "site_id": "site-main",
        "source_node": "master-01",
        "target_node": "fn-02",
        "expires_at_s": time.time() + 60,
        "command": CONFIG_PROPOSE,
        "payload": proposal_payload,
    }
    result = _execute_command(context, command)
    replica = context["state"]["node_replica"]
    if (
        not result.get("ok")
        or result.get("code") != "accepted"
        or replica["revision"] != initial["revision"] + 1
        or replica["fingerprint"] != fingerprint(candidate)
    ):
        raise AssertionError(
            "Node-local proposal self-test failed: "
            + json.dumps(result, ensure_ascii=False, sort_keys=True))
    duplicate = _execute_command(context, command)
    if duplicate != result or context["statistics"]["duplicates"] != 1:
        raise AssertionError("proposal dedupe self-test failed")

    broadcast = copy.deepcopy(command)
    broadcast["message_id"] = "broadcast"
    broadcast["target_node"] = "broadcast"
    if _validate_command(context, broadcast).get("code") != "identity_mismatch":
        raise AssertionError("broadcast target fencing self-test failed")
    stale_revision = copy.deepcopy(command)
    stale_revision["message_id"] = "stale-revision"
    stale_revision["payload"]["proposal_id"] = "proposal-stale-revision"
    stale_revision["payload"]["operation_id"] = "operation-stale-revision"
    if _execute_command(context, stale_revision).get("code") != "stale_revision":
        raise AssertionError("stale scope revision self-test failed")
    stale_epoch = copy.deepcopy(command)
    stale_epoch["message_id"] = "stale-authority-epoch"
    stale_epoch["payload"]["proposal_id"] = "proposal-stale-authority-epoch"
    stale_epoch["payload"]["operation_id"] = "operation-stale-authority-epoch"
    stale_epoch["payload"]["base_authority_epoch"] = 0
    stale_epoch["payload"]["base_scope_revision"] = replica["revision"]
    if _execute_command(context, stale_epoch).get("code") != "authority_epoch_stale":
        raise AssertionError("stale authority epoch self-test failed")
    unsupported = copy.deepcopy(command)
    unsupported["message_id"] = "unsupported-operation-format"
    unsupported["payload"]["proposal_id"] = "proposal-unsupported"
    unsupported["payload"]["operation_id"] = "operation-unsupported"
    unsupported["payload"]["operation_format"] = "legacy_json_pointer"
    unsupported["payload"]["base_scope_revision"] = replica["revision"]
    if _execute_command(context, unsupported).get("code") != "unsupported_operation_format":
        raise AssertionError("unsupported operation format self-test failed")

    target = fixture["target"]
    actuator = {
        "protocol": OUTER_PROTOCOL,
        "kind": "command",
        "message_id": "actuator-valid",
        "site_id": "site-main",
        "source_node": "master-01",
        "target_node": "fn-02",
        "expires_at_s": time.time() + 60,
        "command": "distributed.actuator.execute",
        "payload": {
            "protocol": PROTOCOL_V2,
            "operation_id": "00000000-0000-0000-0000-000000000001",
            "coordinator_term": 3,
            "authority_revision": 8,
            "target_node_scope_revision": replica["revision"],
            "target_node_scope_fingerprint": replica["fingerprint"],
            "target_node_execution_epoch": replica["execution_epoch"],
            "task": {
                "controller_uuid": target["controller_uuid"],
                "actuator_id": target["actuator_id"],
                "control_value": 1,
                "execution_scope": fixture["cross"]["scope"],
                "execution_epoch": 7,
            },
        },
    }
    if not _execute_command(context, actuator).get("ok"):
        raise AssertionError("actuator fence success self-test failed")
    stale_execution = copy.deepcopy(actuator)
    stale_execution["message_id"] = "actuator-stale-execution-epoch"
    stale_execution["payload"]["target_node_execution_epoch"] = 0
    if _execute_command(context, stale_execution).get("code") != "stale_execution_epoch":
        raise AssertionError("actuator execution epoch fencing self-test failed")
    stale_term = copy.deepcopy(actuator)
    stale_term["message_id"] = "actuator-stale-term"
    stale_term["payload"]["coordinator_term"] = 2
    if _execute_command(context, stale_term).get("code") != "stale_authority_term":
        raise AssertionError("actuator coordinator term self-test failed")
    expired = copy.deepcopy(command)
    expired["message_id"] = "expired"
    expired["expires_at_s"] = time.time() - 1
    if _validate_command(context, expired).get("code") != "expired":
        raise AssertionError("TTL self-test failed")
    if fixture.get("cross_site") is not False:
        raise AssertionError("fixture must not claim a positive Cross-Site test")
    if (
        context["statistics"]["proposals_accepted"] != 1
        or context["statistics"]["mirror_requests"] != 1
        or context["statistics"]["mirror_snapshots"] != 2
    ):
        raise AssertionError("node-local protocol statistics self-test failed")
    _broker_signature_self_test()
    print("PASS fn-02 node-local protocol simulator self-test")
    return 0
def _parser():
    default_fixture = pathlib.Path(__file__).with_name("fixtures") / "cross_worker_site-main.json"
    parser = argparse.ArgumentParser(description="fn-02 same-site MQTT protocol simulator")
    parser.add_argument("--config", help="fn-02 runtime JSON with mTLS file references")
    parser.add_argument("--broker-introspection-proof", help="authoritative session proof JSON")
    parser.add_argument("--broker-proof-public-key", help="trusted Ed25519 Broker-oracle key")
    parser.add_argument("--broker-proof-key-sha256", help="out-of-band pinned key SHA-256")
    parser.add_argument("--fixture", default=str(default_fixture))
    parser.add_argument("--run-id", default="e2e-" + str(int(time.time())))
    parser.add_argument("--ready-file")
    parser.add_argument("--result-file")
    parser.add_argument("--duration", type=float, default=900.0)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def _emit(status, **kwargs):
    value = {"version": VERSION, "status": status, "updated_at": _utc_now()}
    value.update(kwargs)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return STATUS_CODES[status]


def main():
    arguments = _parser().parse_args()
    if arguments.self_test:
        return _self_test(arguments.fixture)
    if not arguments.config or not arguments.broker_introspection_proof \
            or not arguments.broker_proof_public_key \
            or not arguments.broker_proof_key_sha256:
        return _emit(
            "BLOCKED",
            detail=("--config, --broker-introspection-proof, "
                    "--broker-proof-public-key and --broker-proof-key-sha256 are required"),
        )
    context = None
    try:
        runtime_config = _load_json(arguments.config, "fn-02 runtime config")
        runtime_config["_config_dir"] = str(
            pathlib.Path(arguments.config).expanduser().resolve().parent
        )
        context = _validate_config(runtime_config)
        context["run_id"] = arguments.run_id
        context["duration_s"] = float(arguments.duration)
        context["fixture"] = _load_json(arguments.fixture, "cross fixture")
        context["topics"] = _topics(context)
        context["state"] = _initial_state(
            _load_node_authority_fixture(context["node_id"]))
        context["state_lock"] = threading.RLock()
        context["dedupe"] = {}
        context["statistics"] = {
            "commands": 0,
            "duplicates": 0,
            "scope_applies": 0,
            "full_applies": 0,
            "patch_applies": 0,
            "proposals_accepted": 0,
            "mirror_requests": 0,
            "mirror_snapshots": 0,
            "actuator_results": 0,
        }
        context["actuator_trace"] = []
        context["stop_event"] = threading.Event()
        context["connected_event"] = threading.Event()
        context["stopping"] = False
        context["last_error"] = None
        context["ready_file"] = arguments.ready_file
        _acquire_lock(context)
        certificate = _validate_certificate(context)
        context["broker_proof_public_key"] = _absolute_file(
            arguments.broker_proof_public_key,
            "Broker proof public key",
            pathlib.Path(arguments.broker_proof_public_key).expanduser().resolve().parent,
        )
        pinned_hash = str(arguments.broker_proof_key_sha256 or "")
        if len(pinned_hash) != 64 or any(
                character not in "0123456789abcdef" for character in pinned_hash):
            raise RuntimeError(
                "BLOCKED: --broker-proof-key-sha256 must be 64 lowercase hex characters"
            )
        context["broker_proof_key_sha256"] = pinned_hash
        context["broker_proof_path"] = str(
            pathlib.Path(arguments.broker_introspection_proof).expanduser().resolve()
        )
        if arguments.check_only:
            try:
                proof = _load_exclusive_broker_proof(context)
                site_isolation = _site_isolation_probe(context)
                return _emit(
                    "PASSED", certificate=certificate, broker_proof=proof,
                    cross_site=False, simulation=True, site_isolation=site_isolation,
                )
            finally:
                _release_lock(context)
        if arguments.duration <= 0:
            raise RuntimeError("BLOCKED: simulator duration must be positive")
        result = _run(context, arguments.duration)
        if arguments.result_file:
            _write_json(arguments.result_file, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        if context:
            _release_lock(context)
        text = str(exc)
        status = "BLOCKED" if text.startswith("BLOCKED:") else "FAILED"
        result = {"status": status, "detail": text.split(":", 1)[-1].strip()}
        if arguments.result_file:
            _write_json(arguments.result_file, dict(result, version=VERSION, updated_at=_utc_now()))
        return _emit(status, detail=result["detail"], cross_site=False, simulation=True)


if __name__ == "__main__":
    raise SystemExit(main())
