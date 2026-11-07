"""Opt-in pinned-TLS LAN receiver with source-scoped, revocable credentials."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import secrets
import ssl
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from personal_hub.metrics.contracts import DeviceEnrollment, MeasurementBatch


class MetricsReceiver:
    def __init__(self, repository, vault, settings_repository, *, blocked=lambda: False):
        self.repository, self.vault = repository, vault
        self.settings = settings_repository
        self.blocked = blocked
        self.host, self.port = "127.0.0.1", 0
        self.thread = self.loop = self.runner = None
        self.enrollments = {}
        self.guard = threading.RLock()
        self.status = "Disabled"
        self.accepting = False
        self.fingerprint = ""
        self._tls = None
        self._rates = {}

    def _identity(self):
        stored = self.vault.get("metrics-tls")
        if stored:
            identity = json.loads(stored)
            cert_pem, key_pem = identity["cert"].encode(), identity["key"].encode()
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Personal Hub Metrics")])
            now = datetime.now(UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=3650))
                .sign(key, hashes.SHA256())
            )
            cert_pem = cert.public_bytes(serialization.Encoding.PEM)
            key_pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self.vault.set(
                "metrics-tls", json.dumps({"cert": cert_pem.decode(), "key": key_pem.decode()})
            )
        cert = x509.load_pem_x509_certificate(cert_pem)
        self.fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # SSLContext requires files; keep plaintext key material only in a
        # short-lived directory and delete it immediately after loading.
        with tempfile.TemporaryDirectory(prefix="personal-hub-tls-") as directory:
            cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            cert_path.write_bytes(cert_pem)
            key_path.write_bytes(key_pem)
            context.load_cert_chain(cert_path, key_path)
        return context

    def start(self, host, port=8765):
        if self.blocked():
            raise ValueError("network activity is paused or Hub is locked")
        address = ipaddress.ip_address(host)
        if (
            not (address.is_private or address.is_loopback)
            or address.is_unspecified
            or address.is_multicast
        ):
            raise ValueError("choose a specific local interface address")
        if not 0 <= port <= 65535:
            raise ValueError("invalid port")
        self.stop()
        self.host, self.port = host, port
        self._tls = self._identity()
        ready = threading.Event()
        errors = []

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._start())
            except Exception:
                errors.append(True)
            finally:
                ready.set()
            if not errors:
                self.loop.run_forever()
            elif self.runner:
                self.loop.run_until_complete(self.runner.cleanup())
            self.loop.close()

        self.thread = threading.Thread(target=run, name="hub-metrics", daemon=True)
        self.thread.start()
        if not ready.wait(10) or errors:
            self.stop()
            self.status = "Receiver could not start"
            raise ValueError(self.status)
        self.status = "Receiving"
        self.accepting = True

    async def _start(self):
        from aiohttp import web

        if self.blocked():
            raise ValueError("network activity is paused")
        slots = asyncio.Semaphore(4)

        @web.middleware
        async def boundary(request, handler):
            if self.blocked():
                return web.json_response({"error": "paused"}, status=503)
            remote = request.remote or "unknown"
            with self.guard:
                now = time.monotonic()
                if len(self._rates) > 256 and remote not in self._rates:
                    return web.json_response({"error": "busy"}, status=429)
                history = self._rates.setdefault(remote, deque())
                while history and history[0] < now - 60:
                    history.popleft()
                if len(history) >= 60:
                    return web.json_response({"error": "rate_limited"}, status=429)
                history.append(now)
            try:
                async with asyncio.timeout(10):
                    async with slots:
                        return await handler(request)
            except (ValueError, KeyError, TypeError):
                return web.json_response({"error": "invalid_request"}, status=400)
            except TimeoutError:
                return web.json_response({"error": "timeout"}, status=408)
            except web.HTTPException:
                raise
            except Exception:
                # Exception messages can contain private database parameters.
                # Never pass them to aiohttp's default exception logger.
                return web.json_response({"error": "operation_failed"}, status=500)

        app = web.Application(client_max_size=1024 * 1024, middlewares=[boundary])
        app.router.add_post("/v1/pair", self._pair)
        app.router.add_post("/v1/measurements/batches", self._ingest)
        app.router.add_get("/v1/status", self._status)
        self.runner = web.AppRunner(
            app, access_log=None, shutdown_timeout=2, handler_cancellation=True
        )
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port, ssl_context=self._tls, backlog=16)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    def enrollment(self, source_id):
        if self.blocked() or self.status != "Receiving":
            raise ValueError("start the receiver first")
        source = self.repository.source(source_id)
        if source is None or source.revoked:
            raise ValueError("approve the source and permitted metrics first")
        token = secrets.token_urlsafe(32)
        with self.guard:
            self.enrollments.clear()
            self.enrollments[token] = (source_id, time.monotonic() + 300)
        host = f"[{self.host}]" if ":" in self.host else self.host
        return json.dumps(
            {
                "url": f"https://{host}:{self.port}",
                "fingerprint": self.fingerprint,
                "token": token,
                "source_id": source_id,
            },
            separators=(",", ":"),
        )

    def _credential_name(self, source_id):
        return "metrics-" + hashlib.sha256(source_id.encode()).hexdigest()[:40]

    async def _pair(self, request):
        from aiohttp import web

        claim = DeviceEnrollment.model_validate(await request.json())
        with self.guard:
            entry = self.enrollments.pop(claim.token, None)
            if not entry or entry[0] != claim.source_id or entry[1] < time.monotonic():
                return web.json_response({"error": "enrollment_expired"}, status=403)
            source = self.repository.source(claim.source_id)
            if source is None or source.revoked or self.blocked():
                return web.json_response({"error": "source_unavailable"}, status=403)
            credential = secrets.token_urlsafe(32)
            self.vault.set(self._credential_name(claim.source_id), credential)
        return web.json_response({"source_id": claim.source_id, "credential": credential})

    def _authorize(self, request):
        source_id = request.headers.get("X-Hub-Source", "")
        if len(source_id) > 128:
            return None
        source = self.repository.source(source_id)
        if source is None or source.revoked:
            return None
        expected = self.vault.get(self._credential_name(source_id))
        given = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return source_id if expected and secrets.compare_digest(expected, given) else None

    async def _ingest(self, request):
        from aiohttp import web

        source_id = self._authorize(request)
        if source_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        batch = MeasurementBatch.model_validate(await request.json())
        if batch.source_id != source_id:
            return web.json_response({"error": "source_mismatch"}, status=403)
        # Serialized bounded transactions cannot outlive the pause/stop boundary.
        if self.blocked():
            return web.json_response({"error": "paused"}, status=503)
        return web.json_response(
            self.repository.ingest(batch, allowed=lambda: self.accepting and not self.blocked())
        )

    async def _status(self, request):
        from aiohttp import web

        if self._authorize(request) is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"format": "personal-hub-metrics-v1", "state": "ready"})

    def revoke(self, source_id):
        with self.guard:
            self.repository.revoke(source_id)
            self.vault.delete(self._credential_name(source_id))
            self.enrollments.clear()

    def stop(self):
        self.accepting = False
        with self.guard:
            self.enrollments.clear()
        loop, thread = self.loop, self.thread
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.runner.cleanup(), loop)
            try:
                future.result(timeout=5)
            finally:
                loop.call_soon_threadsafe(loop.stop)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=6)
        self.loop = self.thread = self.runner = None
        self.status = "Disabled"
