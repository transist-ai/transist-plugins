"""Provision disposable secrets, TLS, documents and a pinned model before runs."""

import datetime
import argparse
import hashlib
import ipaddress
import json
import os
import secrets
from pathlib import Path

import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fixtures import write_documents
from provider import FILES, LOCK, REPOSITORY, REVISION


def provision(root: Path = Path("/assets"), *, tika_only: bool = False) -> None:
    """Populate an empty fixture volume; never emit private values."""
    if list(root.iterdir()):
        raise RuntimeError("Provisioning requires an empty fixture volume")
    credentials = {
        name: secrets.token_urlsafe(32)
        for name in (
            ("tika",)
            if tika_only
            else ("gateway", "tika", "database_admin", "database_readonly")
        )
    }
    private = root / "credentials.json"
    private.write_text(json.dumps(credentials))
    private.chmod(0o600)
    if not tika_only:
        config = root / "qdrant.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "service": {
                        "api_key": credentials["database_admin"],
                        "read_only_api_key": credentials["database_readonly"],
                    },
                    "telemetry_disabled": True,
                }
            )
        )
        config.chmod(0o600)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fixture CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway")]))
        .issuer_name(subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("gateway"),
                    *([] if tika_only else [x509.DNSName("localhost")]),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (root / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (root / "server.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    private_key = root / "server.key"
    private_key.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_key.chmod(0o600)
    write_documents(root / "documents")
    if tika_only:
        # The production service image is non-root. No model or database assets
        # are needed, and localhost deliberately tests hostname verification.
        os.chown(private, 65534, 65534)
        os.chown(private_key, 65534, 65534)
        return
    model = root / "model"
    model.mkdir()
    manifest = {"repository": REPOSITORY, "revision": REVISION, "sha256": {}}
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=180) as client:
        for name in FILES:
            url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
            response = client.get(url)
            response.raise_for_status()
            data = response.content
            if hashlib.sha256(data).hexdigest() != LOCK["sha256"][name]:
                raise RuntimeError("Pinned model download hash mismatch")
            (model / name).write_bytes(data)
            manifest["sha256"][name] = hashlib.sha256(data).hexdigest()
    (model / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tika-only", action="store_true")
    provision(tika_only=parser.parse_args().tika_only)
