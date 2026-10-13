"""Self-signed TLS so the web UI can use the microphone.

Browsers only expose ``navigator.mediaDevices`` on secure origins (HTTPS or
localhost).  The add-on is normally reached over plain HTTP, which is why
press-to-talk reports that the microphone is unavailable.  Serving the same
UI over HTTPS with a self-signed certificate fixes that: once the browser
warning is accepted the origin counts as secure.

The certificate is generated on first use with the ``openssl`` binary that is
already present in the add-on image, and is stored under /data so it survives
restarts.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import ssl
import subprocess

_LOGGER = logging.getLogger("hikbridge")

DEFAULT_SSL_DIR = "/data/ssl"
DEFAULT_CERT_NAME = "hikbridge.crt"
DEFAULT_KEY_NAME = "hikbridge.key"


def _local_addresses() -> list[str]:
    """Addresses the browser is likely to use to reach this add-on."""
    addresses = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    # The address used for outbound traffic is the LAN address in most setups.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return sorted(addresses)


def _generate(cert_path: str, key_path: str) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "openssl is not installed, cannot create a self-signed certificate"
        )
    san = ",".join(["DNS:localhost"] + [f"IP:{ip}" for ip in _local_addresses()])
    os.makedirs(os.path.dirname(cert_path) or ".", exist_ok=True)
    command = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", cert_path,
        "-days", "3650",
        "-subj", "/CN=hikbridge",
        "-addext", f"subjectAltName={san}",
    ]
    _LOGGER.info("creating a self-signed certificate for %s", san)
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)


def build_context(certfile: str | None = None, keyfile: str | None = None) -> ssl.SSLContext:
    """Return an SSLContext, generating a self-signed pair when needed."""
    cert_path = certfile or os.path.join(DEFAULT_SSL_DIR, DEFAULT_CERT_NAME)
    key_path = keyfile or os.path.join(DEFAULT_SSL_DIR, DEFAULT_KEY_NAME)
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        _generate(cert_path, key_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    _LOGGER.info("HTTPS enabled with %s", cert_path)
    return context
