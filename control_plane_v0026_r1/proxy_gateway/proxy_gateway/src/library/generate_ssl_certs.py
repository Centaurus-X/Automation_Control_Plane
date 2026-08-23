# -*- coding: utf-8 -*-

# proxy_gateway/proxy_gateway/src/library/generate_ssl_certs.py

"""WSS TLS loading and deterministic, explicit lab-PKI generation.

What it is:
    A functional TLS helper for the standalone ``websockets`` listener.  The
    runtime part only loads explicitly supplied certificate files.  The lab
    part can create a small CA and one CA-signed server certificate.

Why it is needed:
    The Proxy must fail closed on missing or invalid production secrets while
    still offering a reproducible test-PKI workflow.  Bind addresses and
    certificate identities are deliberately kept separate.

How it works:
    Explicit DNS and IP SANs are normalized and the one-level lab chain is
    validated.  Lab material is staged under an exclusive process lock and
    replaced per file.  Private keys are mode 0600.  Production material is
    never generated, rotated or chain-validated by this helper; the runtime
    only loads an externally provisioned cert chain and key.
"""


# =============================================================================
# MODULE IMPORTS
# =============================================================================

import argparse
import datetime
import ipaddress
import json
import os
import re
import shutil
import ssl
import stat
import tempfile

from functools import partial


# =============================================================================
# CONSTANTS
# =============================================================================

DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
FORBIDDEN_SAN_IDENTITIES = {"0.0.0.0", "::", "*"}
DEFAULT_CA_NAME = "Automation Control Plane v0017 Lab CA"
DEFAULT_SERVER_NAME = "Automation Proxy WSS v0017"


# =============================================================================
# GENERIC HELPERS
# =============================================================================

def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _json_ready(value):
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _safe_control_text(value, label):
    text = str(value or "").strip()
    if not text:
        raise ValueError(label + " must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(label + " contains a control character")
    return text


def normalize_dns_names(values):
    """Return deterministic IDNA DNS SANs and reject wildcards."""
    result = []
    for supplied in values or ():
        text = _safe_control_text(supplied, "DNS SAN").rstrip(".").lower()
        if text in FORBIDDEN_SAN_IDENTITIES or "*" in text:
            raise ValueError("wildcard or bind-only DNS SAN is forbidden: " + text)
        try:
            ascii_name = text.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("DNS SAN is not valid IDNA: " + text) from exc
        if len(ascii_name) > 253:
            raise ValueError("DNS SAN is too long: " + ascii_name)
        labels = ascii_name.split(".")
        if not labels or any(not DNS_LABEL_PATTERN.fullmatch(label) for label in labels):
            raise ValueError("DNS SAN has an invalid label: " + ascii_name)
        if ascii_name not in result:
            result.append(ascii_name)
    return tuple(sorted(result))


def normalize_ip_addresses(values):
    """Return deterministic canonical IP SANs."""
    result = []
    for supplied in values or ():
        text = _safe_control_text(supplied, "IP SAN")
        if text in FORBIDDEN_SAN_IDENTITIES:
            raise ValueError("bind-only IP SAN is forbidden: " + text)
        normalized = str(ipaddress.ip_address(text))
        if normalized not in result:
            result.append(normalized)
    return tuple(sorted(result))


def normalize_server_identities(**kwargs):
    dns_names = normalize_dns_names(kwargs.get("dns_names"))
    ip_addresses = normalize_ip_addresses(kwargs.get("ip_addresses"))
    if not dns_names and not ip_addresses:
        raise ValueError("at least one explicit DNS or IP SAN is required")
    return {"dns_names": dns_names, "ip_addresses": ip_addresses}


def _regular_file(path_value, label):
    path = os.path.abspath(os.path.expanduser(str(path_value or "")))
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(label + " does not exist: " + path)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(label + " must be a regular non-symlink file")
    if info.st_nlink != 1:
        raise ValueError(label + " must not have hard links")
    return path


def _validate_key_permissions(path_value, label):
    path = _regular_file(path_value, label)
    if os.name == "nt":
        # Windows protects keys with ACLs; POSIX mode bits are synthetic.
        return path
    info = os.stat(path)
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o037:
        raise PermissionError(
            "{} must not be group-writable/executable or world-accessible, got {:04o}".format(
                label, mode))
    if mode & 0o040:
        trusted_groups = set(os.getgroups())
        trusted_groups.add(os.getegid())
        if info.st_gid not in trusted_groups:
            raise PermissionError(
                "{} group-readable mode requires the runtime group".format(label))
    return path


def _ensure_private_directory(path_value):
    path = os.path.abspath(os.path.expanduser(str(path_value)))
    if os.path.lexists(path) and os.path.islink(path):
        raise ValueError("output directory must not be a symlink")
    os.makedirs(path, mode=0o700, exist_ok=True)
    if not os.path.isdir(path):
        raise ValueError("output path is not a directory: " + path)
    os.chmod(path, 0o700)
    return path


def _write_exclusive(path_value, payload, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path_value, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path_value, mode)


def _paths(output_directory):
    root = os.path.abspath(output_directory)
    return {
        "output_directory": root,
        "lock": os.path.join(root, ".wss-pki.lock"),
        "ca_key": os.path.join(root, "ca.key"),
        "ca_cert": os.path.join(root, "ca.crt"),
        "server_key": os.path.join(root, "proxy_wss.key"),
        "server_cert": os.path.join(root, "proxy_wss.crt"),
        "fullchain": os.path.join(root, "proxy_wss.fullchain.crt"),
        "manifest": os.path.join(root, "manifest.json"),
    }


def _lock_backend():
    try:
        import fcntl
        return {"name": "fcntl", "module": fcntl}
    except ImportError:
        try:
            import msvcrt
            return {"name": "msvcrt", "module": msvcrt}
        except ImportError as exc:
            raise RuntimeError(
                "secure lab-PKI generation requires an OS file lock") from exc


def _acquire_platform_lock(descriptor, backend, empty):
    module = backend["module"]
    if backend["name"] == "fcntl":
        module.flock(descriptor, module.LOCK_EX)
        return
    if empty:
        os.write(descriptor, bytes((0,)))
        os.fsync(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    module.locking(descriptor, module.LK_LOCK, 1)


def _exclusive_lock(path_value):
    backend = _lock_backend()
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path_value, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("lab-PKI lock must be a regular non-linked file")
    os.chmod(path_value, 0o600)
    try:
        _acquire_platform_lock(descriptor, backend, info.st_size == 0)
    except Exception:
        os.close(descriptor)
        raise
    return {"descriptor": descriptor, "backend": backend}


def _release_lock(lock):
    if not lock:
        return
    descriptor = lock["descriptor"]
    backend = lock["backend"]
    module = backend["module"]
    try:
        if backend["name"] == "fcntl":
            module.flock(descriptor, module.LOCK_UN)
        else:
            os.lseek(descriptor, 0, os.SEEK_SET)
            module.locking(descriptor, module.LK_UNLCK, 1)
    finally:
        os.close(descriptor)


# =============================================================================
# CRYPTOGRAPHY HELPERS
# =============================================================================

def _load_crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:
        raise RuntimeError(
            "certificate generation and policy validation require cryptography"
        ) from exc
    return {
        "x509": x509,
        "hashes": hashes,
        "serialization": serialization,
        "ec": ec,
        "padding": padding,
        "rsa": rsa,
        "eku_oid": ExtendedKeyUsageOID,
        "name_oid": NameOID,
    }


def _pem_private_key(key, crypto):
    return key.private_bytes(
        encoding=crypto["serialization"].Encoding.PEM,
        format=crypto["serialization"].PrivateFormat.PKCS8,
        encryption_algorithm=crypto["serialization"].NoEncryption(),
    )


def _pem_certificate(certificate, crypto):
    return certificate.public_bytes(crypto["serialization"].Encoding.PEM)


def _certificate_window(certificate):
    not_before = getattr(certificate, "not_valid_before_utc", None)
    not_after = getattr(certificate, "not_valid_after_utc", None)
    if not_before is None:
        not_before = certificate.not_valid_before.replace(tzinfo=datetime.timezone.utc)
    if not_after is None:
        not_after = certificate.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    return not_before, not_after


def _verify_signature(certificate, public_key, crypto):
    if isinstance(public_key, crypto["rsa"].RSAPublicKey):
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            crypto["padding"].PKCS1v15(),
            certificate.signature_hash_algorithm,
        )
        return
    if isinstance(public_key, crypto["ec"].EllipticCurvePublicKey):
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            crypto["ec"].ECDSA(certificate.signature_hash_algorithm),
        )
        return
    raise ValueError("unsupported certificate public-key algorithm")


def _public_key_bytes(key, crypto):
    return key.public_bytes(
        encoding=crypto["serialization"].Encoding.DER,
        format=crypto["serialization"].PublicFormat.SubjectPublicKeyInfo,
    )


def _read_material(**kwargs):
    crypto = _load_crypto()
    cert_path = _regular_file(kwargs["certfile"], "server certificate")
    key_path = _validate_key_permissions(kwargs["keyfile"], "server private key")
    ca_path = _regular_file(kwargs["cafile"], "CA certificate")
    with open(cert_path, "rb") as handle:
        certificate = crypto["x509"].load_pem_x509_certificate(handle.read())
    with open(key_path, "rb") as handle:
        private_key = crypto["serialization"].load_pem_private_key(
            handle.read(), password=kwargs.get("password"))
    with open(ca_path, "rb") as handle:
        ca_certificate = crypto["x509"].load_pem_x509_certificate(handle.read())
    return {
        "crypto": crypto,
        "certificate": certificate,
        "private_key": private_key,
        "ca_certificate": ca_certificate,
        "certfile": cert_path,
        "keyfile": key_path,
        "cafile": ca_path,
    }


def _certificate_sans(certificate, crypto):
    extension = certificate.extensions.get_extension_for_class(
        crypto["x509"].SubjectAlternativeName)
    dns_names = extension.value.get_values_for_type(crypto["x509"].DNSName)
    ip_values = extension.value.get_values_for_type(crypto["x509"].IPAddress)
    return {
        "dns_names": normalize_dns_names(dns_names),
        "ip_addresses": tuple(sorted(str(value) for value in ip_values)),
    }


def _validate_certificate_policy(material, **kwargs):
    crypto = material["crypto"]
    certificate = material["certificate"]
    ca_certificate = material["ca_certificate"]
    private_key = material["private_key"]
    expected = normalize_server_identities(
        dns_names=kwargs.get("dns_names"), ip_addresses=kwargs.get("ip_addresses"))

    if _public_key_bytes(certificate.public_key(), crypto) != _public_key_bytes(
            private_key.public_key(), crypto):
        raise ValueError("server certificate and private key do not match")
    if certificate.issuer != ca_certificate.subject:
        raise ValueError("server certificate issuer does not match the CA")
    _verify_signature(certificate, ca_certificate.public_key(), crypto)
    if ca_certificate.issuer != ca_certificate.subject:
        raise ValueError("lab CA must be self-issued")
    _verify_signature(ca_certificate, ca_certificate.public_key(), crypto)

    ca_constraints = ca_certificate.extensions.get_extension_for_class(
        crypto["x509"].BasicConstraints).value
    leaf_constraints = certificate.extensions.get_extension_for_class(
        crypto["x509"].BasicConstraints).value
    if not ca_constraints.ca or ca_constraints.path_length not in (0, None):
        raise ValueError("CA certificate does not have the expected CA constraints")
    if leaf_constraints.ca:
        raise ValueError("server certificate must have CA:FALSE")

    eku = certificate.extensions.get_extension_for_class(
        crypto["x509"].ExtendedKeyUsage).value
    if tuple(eku) != (crypto["eku_oid"].SERVER_AUTH,):
        raise ValueError("server certificate EKU must contain serverAuth only")
    key_usage = certificate.extensions.get_extension_for_class(
        crypto["x509"].KeyUsage).value
    if not key_usage.digital_signature or key_usage.key_cert_sign:
        raise ValueError("server certificate KeyUsage is invalid")

    public_key = certificate.public_key()
    if isinstance(public_key, crypto["rsa"].RSAPublicKey) and public_key.key_size < 3072:
        raise ValueError("RSA server key must have at least 3072 bits")
    if isinstance(public_key, crypto["ec"].EllipticCurvePublicKey) and public_key.key_size < 256:
        raise ValueError("EC server key must have at least 256 bits")

    ca_key_usage = ca_certificate.extensions.get_extension_for_class(
        crypto["x509"].KeyUsage).value
    if not ca_key_usage.key_cert_sign or not ca_key_usage.crl_sign:
        raise ValueError("lab CA KeyUsage must permit certificate and CRL signing")
    ca_public_key = ca_certificate.public_key()
    if isinstance(ca_public_key, crypto["rsa"].RSAPublicKey) and ca_public_key.key_size < 4096:
        raise ValueError("lab CA RSA key must have at least 4096 bits")
    if isinstance(ca_public_key, crypto["ec"].EllipticCurvePublicKey) and ca_public_key.key_size < 256:
        raise ValueError("lab CA EC key must have at least 256 bits")

    now = _utc_now()
    not_before, not_after = _certificate_window(certificate)
    ca_not_before, ca_not_after = _certificate_window(ca_certificate)
    renew_before = datetime.timedelta(days=max(0, int(kwargs.get("renew_before_days", 0))))
    if now < not_before:
        raise ValueError("server certificate is not valid yet")
    if now >= not_after - renew_before:
        raise ValueError("server certificate is expired or inside the renewal window")
    if now < ca_not_before:
        raise ValueError("lab CA certificate is not valid yet")
    if now >= ca_not_after - renew_before:
        raise ValueError("lab CA certificate is expired or inside the renewal window")
    if not_after > ca_not_after:
        raise ValueError("server certificate outlives its lab CA")

    actual = _certificate_sans(certificate, crypto)
    exact = bool(kwargs.get("exact_sans", True))
    if exact and actual != expected:
        raise ValueError("server certificate SAN set does not match the expected set")
    if not exact:
        if not set(expected["dns_names"]).issubset(actual["dns_names"]):
            raise ValueError("server certificate is missing an expected DNS SAN")
        if not set(expected["ip_addresses"]).issubset(actual["ip_addresses"]):
            raise ValueError("server certificate is missing an expected IP SAN")
    return {
        "ok": True,
        "changed": False,
        "action": "validated",
        "certfile": material["certfile"],
        "keyfile": material["keyfile"],
        "cafile": material["cafile"],
        "dns_names": actual["dns_names"],
        "ip_addresses": actual["ip_addresses"],
        "not_before": not_before,
        "not_after": not_after,
        "ca_not_before": ca_not_before,
        "ca_not_after": ca_not_after,
        "serial_number": str(certificate.serial_number),
    }


# =============================================================================
# PUBLIC VALIDATION AND TLS CONTEXT
# =============================================================================

def validate_lab_websocket_server_ssl_material(**kwargs):
    """Validate the explicit one-level lab CA, leaf and key without writes."""
    material = _read_material(**kwargs)
    result = _validate_certificate_policy(material, **kwargs)
    context = build_server_ssl_context(
        certfile=result["certfile"], keyfile=result["keyfile"],
        cafile=kwargs.get("client_cafile"),
        require_client_cert=kwargs.get("require_client_cert", False),
        minimum_tls_version=kwargs.get("minimum_tls_version", "TLSv1_2"),
    )
    if not isinstance(context, ssl.SSLContext):
        raise ValueError("server TLS context could not be constructed")
    return result


def build_server_ssl_context(**kwargs):
    """Build the WSS server context; this function never generates material."""
    certfile = _regular_file(kwargs.get("certfile"), "server certificate")
    keyfile = _validate_key_permissions(kwargs.get("keyfile"), "server private key")
    minimum = str(kwargs.get("minimum_tls_version") or "TLSv1_2")
    versions = {"TLSv1_2": ssl.TLSVersion.TLSv1_2, "TLSv1_3": ssl.TLSVersion.TLSv1_3}
    if minimum not in versions:
        raise ValueError("unsupported minimum TLS version: " + minimum)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = versions[minimum]
    context.options |= getattr(ssl, "OP_NO_COMPRESSION", 0)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile, password=kwargs.get("password"))
    context.set_alpn_protocols(["http/1.1"])
    ciphers = str(kwargs.get("ciphers") or "").strip()
    if ciphers:
        context.set_ciphers(ciphers)
    if kwargs.get("require_client_cert"):
        cafile = _regular_file(kwargs.get("cafile"), "client CA certificate")
        context.load_verify_locations(cafile=cafile)
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.verify_mode = ssl.CERT_NONE
    return context


# =============================================================================
# EXPLICIT LAB PKI GENERATION
# =============================================================================

def _build_ca(**kwargs):
    crypto = kwargs["crypto"]
    now = _utc_now()
    key = crypto["rsa"].generate_private_key(public_exponent=65537, key_size=4096)
    name = crypto["x509"].Name([
        crypto["x509"].NameAttribute(
            crypto["name_oid"].COMMON_NAME,
            _safe_control_text(kwargs.get("common_name") or DEFAULT_CA_NAME, "CA name")),
    ])
    certificate = (
        crypto["x509"].CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(crypto["x509"].random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=int(kwargs.get("valid_days", 365))))
        .add_extension(crypto["x509"].BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(crypto["x509"].KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(crypto["x509"].SubjectKeyIdentifier.from_public_key(
            key.public_key()), critical=False)
        .sign(key, crypto["hashes"].SHA256())
    )
    return {"key": key, "certificate": certificate}


def _build_server_leaf(**kwargs):
    crypto = kwargs["crypto"]
    ca_key = kwargs["ca_key"]
    ca_certificate = kwargs["ca_certificate"]
    identities = kwargs["identities"]
    now = _utc_now()
    key = crypto["rsa"].generate_private_key(public_exponent=65537, key_size=3072)
    name = crypto["x509"].Name([
        crypto["x509"].NameAttribute(
            crypto["name_oid"].COMMON_NAME,
            _safe_control_text(kwargs.get("common_name") or DEFAULT_SERVER_NAME, "server name")),
    ])
    san_entries = [crypto["x509"].DNSName(name) for name in identities["dns_names"]]
    san_entries.extend(crypto["x509"].IPAddress(ipaddress.ip_address(value))
                       for value in identities["ip_addresses"])
    certificate = (
        crypto["x509"].CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(crypto["x509"].random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=int(kwargs.get("valid_days", 30))))
        .add_extension(crypto["x509"].BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(crypto["x509"].KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(crypto["x509"].ExtendedKeyUsage([
            crypto["eku_oid"].SERVER_AUTH]), critical=False)
        .add_extension(crypto["x509"].SubjectAlternativeName(san_entries), critical=False)
        .add_extension(crypto["x509"].SubjectKeyIdentifier.from_public_key(
            key.public_key()), critical=False)
        .add_extension(crypto["x509"].AuthorityKeyIdentifier.from_issuer_public_key(
            ca_key.public_key()), critical=False)
        .sign(ca_key, crypto["hashes"].SHA256())
    )
    return {"key": key, "certificate": certificate}


def _load_ca(paths, crypto):
    with open(_validate_key_permissions(paths["ca_key"], "CA private key"), "rb") as handle:
        key = crypto["serialization"].load_pem_private_key(handle.read(), password=None)
    with open(_regular_file(paths["ca_cert"], "CA certificate"), "rb") as handle:
        certificate = crypto["x509"].load_pem_x509_certificate(handle.read())
    if _public_key_bytes(certificate.public_key(), crypto) != _public_key_bytes(
            key.public_key(), crypto):
        raise ValueError("CA certificate and private key do not match")
    return {"key": key, "certificate": certificate}


def _lab_ca_can_cover_leaf(ca, valid_days):
    not_before, not_after = _certificate_window(ca["certificate"])
    now = _utc_now()
    requested_end = now + datetime.timedelta(days=int(valid_days))
    return bool(not_before <= now and requested_end <= not_after)


def _validate_bundle_files(paths, validation):
    fullchain_path = _regular_file(paths["fullchain"], "lab fullchain")
    manifest_path = _regular_file(paths["manifest"], "lab manifest")
    with open(_regular_file(paths["server_cert"], "server certificate"), "rb") as handle:
        leaf_pem = handle.read()
    with open(_regular_file(paths["ca_cert"], "CA certificate"), "rb") as handle:
        ca_pem = handle.read()
    with open(fullchain_path, "rb") as handle:
        if handle.read() != leaf_pem + ca_pem:
            raise ValueError("lab fullchain does not match leaf plus CA")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    expected = {
        "schema": "automation.proxy.wss.lab-pki.v1",
        "dns_names": list(validation["dns_names"]),
        "ip_addresses": list(validation["ip_addresses"]),
        "server_serial_number": validation["serial_number"],
        "test_only": True,
    }
    for name, value in expected.items():
        if manifest.get(name) != value:
            raise ValueError("lab manifest field does not match: " + name)
    return True


def _activate_staged_files(staged, paths, include_ca):
    names = ["server_key", "server_cert", "fullchain", "manifest"]
    if include_ca:
        names = ["ca_key", "ca_cert"] + names
    for name in names:
        os.replace(staged[name], paths[name])
    os.chmod(paths["server_key"], 0o600)
    os.chmod(paths["server_cert"], 0o644)
    os.chmod(paths["fullchain"], 0o644)
    os.chmod(paths["manifest"], 0o600)
    if include_ca:
        os.chmod(paths["ca_key"], 0o600)
        os.chmod(paths["ca_cert"], 0o644)


def generate_lab_websocket_pki(**kwargs):
    """Generate or rotate explicit lab material; never call this in production."""
    if not kwargs.get("test_only"):
        raise PermissionError("lab PKI generation requires test_only=True")
    identities = normalize_server_identities(
        dns_names=kwargs.get("dns_names"), ip_addresses=kwargs.get("ip_addresses"))
    output_directory = _ensure_private_directory(kwargs["output_directory"])
    paths = _paths(output_directory)
    lock = _exclusive_lock(paths["lock"])
    stage_directory = None
    try:
        material_names = (
            "ca_key", "ca_cert", "server_key", "server_cert", "fullchain", "manifest")
        have_all = all(os.path.isfile(paths[name]) for name in material_names)
        if have_all and not kwargs.get("rotate"):
            try:
                existing = validate_lab_websocket_server_ssl_material(
                    certfile=paths["server_cert"], keyfile=paths["server_key"],
                    cafile=paths["ca_cert"], dns_names=identities["dns_names"],
                    ip_addresses=identities["ip_addresses"], exact_sans=True,
                    renew_before_days=kwargs.get("renew_before_days", 0))
                _validate_bundle_files(paths, existing)
                existing["paths"] = paths
                return existing
            except Exception:
                raise
        elif not have_all and any(os.path.lexists(paths[name]) for name in material_names):
            raise ValueError("partial lab PKI exists; manual recovery is required")

        crypto = _load_crypto()
        include_ca = not have_all
        ca = None if include_ca else _load_ca(paths, crypto)
        if ca and not _lab_ca_can_cover_leaf(ca, kwargs.get("valid_days", 30)):
            include_ca = True
        if include_ca:
            ca = _build_ca(
                crypto=crypto, valid_days=kwargs.get("ca_valid_days", 365),
                common_name=kwargs.get("ca_common_name"))
        leaf = _build_server_leaf(
            crypto=crypto, ca_key=ca["key"], ca_certificate=ca["certificate"],
            identities=identities, valid_days=kwargs.get("valid_days", 30),
            common_name=kwargs.get("server_common_name"))

        stage_directory = tempfile.mkdtemp(prefix=".wss-pki-stage-", dir=output_directory)
        os.chmod(stage_directory, 0o700)
        staged = {name: os.path.join(stage_directory, os.path.basename(path))
                  for name, path in paths.items() if name not in ("output_directory", "lock")}
        if include_ca:
            _write_exclusive(staged["ca_key"], _pem_private_key(ca["key"], crypto), 0o600)
            _write_exclusive(staged["ca_cert"], _pem_certificate(ca["certificate"], crypto), 0o644)
        _write_exclusive(staged["server_key"], _pem_private_key(leaf["key"], crypto), 0o600)
        leaf_pem = _pem_certificate(leaf["certificate"], crypto)
        ca_pem = _pem_certificate(ca["certificate"], crypto)
        _write_exclusive(staged["server_cert"], leaf_pem, 0o644)
        _write_exclusive(staged["fullchain"], leaf_pem + ca_pem, 0o644)
        manifest = {
            "schema": "automation.proxy.wss.lab-pki.v1",
            "created_at": _utc_now(),
            "dns_names": identities["dns_names"],
            "ip_addresses": identities["ip_addresses"],
            "server_serial_number": str(leaf["certificate"].serial_number),
            "test_only": True,
        }
        _write_exclusive(staged["manifest"], json.dumps(
            _json_ready(manifest), sort_keys=True, indent=2).encode("utf-8") + b"\n", 0o600)

        validation_ca = staged["ca_cert"] if include_ca else paths["ca_cert"]
        validate_lab_websocket_server_ssl_material(
            certfile=staged["server_cert"], keyfile=staged["server_key"],
            cafile=validation_ca, dns_names=identities["dns_names"],
            ip_addresses=identities["ip_addresses"], exact_sans=True)
        _activate_staged_files(staged, paths, include_ca)
        result = validate_lab_websocket_server_ssl_material(
            certfile=paths["server_cert"], keyfile=paths["server_key"],
            cafile=paths["ca_cert"], dns_names=identities["dns_names"],
            ip_addresses=identities["ip_addresses"], exact_sans=True)
        _validate_bundle_files(paths, result)
        result.update({"changed": True, "action": "generated" if include_ca else "rotated",
                       "paths": paths})
        return result
    finally:
        if stage_directory and os.path.isdir(stage_directory):
            shutil.rmtree(stage_directory)
        _release_lock(lock)


def ensure_websocket_server_ssl_material(**kwargs):
    """Lab-tool compatibility entry point with validation as the default."""
    if kwargs.get("development_generate"):
        supplied = dict(kwargs)
        supplied["test_only"] = bool(kwargs.get("test_only"))
        supplied.pop("development_generate", None)
        return generate_lab_websocket_pki(**supplied)
    return validate_lab_websocket_server_ssl_material(**kwargs)


# =============================================================================
# CLI
# =============================================================================

def _parser():
    parser = argparse.ArgumentParser(prog="generate_ssl_certs.py")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("lab-generate")
    generate.add_argument("--out-dir", required=True)
    generate.add_argument("--dns", action="append", default=[])
    generate.add_argument("--ip", action="append", default=[])
    generate.add_argument("--valid-days", type=int, default=30)
    generate.add_argument("--renew-before-days", type=int, default=0)
    generate.add_argument("--rotate", action="store_true")
    generate.add_argument("--test-only", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("--cert", required=True)
    validate.add_argument("--key", required=True)
    validate.add_argument("--ca", required=True)
    validate.add_argument("--expected-dns", action="append", default=[])
    validate.add_argument("--expected-ip", action="append", default=[])
    validate.add_argument("--renew-before-days", type=int, default=0)
    return parser


def main():
    arguments = _parser().parse_args()
    try:
        if arguments.command == "lab-generate":
            result = generate_lab_websocket_pki(
                output_directory=arguments.out_dir, dns_names=arguments.dns,
                ip_addresses=arguments.ip, valid_days=arguments.valid_days,
                renew_before_days=arguments.renew_before_days,
                rotate=arguments.rotate, test_only=arguments.test_only)
        else:
            result = validate_lab_websocket_server_ssl_material(
                certfile=arguments.cert, keyfile=arguments.key, cafile=arguments.ca,
                dns_names=arguments.expected_dns, ip_addresses=arguments.expected_ip,
                renew_before_days=arguments.renew_before_days, exact_sans=True)
    except (FileNotFoundError, PermissionError) as exc:
        print(json.dumps({"ok": False, "code": "file_policy", "message": str(exc)}))
        return 66
    except Exception as exc:
        print(json.dumps({"ok": False, "code": "invalid_material", "message": str(exc)}))
        return 65
    print(json.dumps(_json_ready(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
