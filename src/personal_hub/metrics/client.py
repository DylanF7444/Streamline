"""Small pinned-TLS client for external, user-owned measurement senders."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import secrets
import ssl
from urllib.parse import urlsplit


def post(connection, path, payload):
    url = urlsplit(connection["url"])
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("expected a root HTTPS address")
    address = ipaddress.ip_address(url.hostname)
    if (
        not (address.is_private or address.is_loopback)
        or address.is_unspecified
        or address.is_multicast
    ):
        raise ValueError("root address must be a specific local interface")
    body = json.dumps(payload, separators=(",", ":")).encode()
    if len(body) > 1024 * 1024:
        raise ValueError("split the batch into smaller requests")
    # Identity is established by the out-of-band fingerprint before sending
    # either an enrollment token or a source credential.
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    client = http.client.HTTPSConnection(url.hostname, url.port or 443, context=tls, timeout=10)
    try:
        client.connect()
        actual = hashlib.sha256(client.sock.getpeercert(binary_form=True)).hexdigest()
        if not secrets.compare_digest(actual, connection["fingerprint"].lower()):
            raise ValueError("root certificate fingerprint changed")
        headers = {"Content-Type": "application/json", "X-Hub-Source": connection["source_id"]}
        if "credential" in connection:
            headers["Authorization"] = "Bearer " + connection["credential"]
        client.request("POST", path, body, headers)
        response = client.getresponse()
        raw = response.read(65537)
        if not 200 <= response.status < 300 or len(raw) > 65536:
            raise ValueError("root rejected transfer or returned an invalid response")
        return json.loads(raw)
    finally:
        client.close()
