"""Explicit standard-library JSON/HTTP transport for remote workers."""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import (HTTPRedirectHandler, HTTPSHandler, Request,
                            build_opener)

from tabularium.engine.projection.budget import measure

from .model import RemoteCancellation, RemoteEnvelope

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class RemoteTransportFailure(Exception):
    """A controlled transport failure; no retry or fallback is implied."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail[:300]


class _RefuseRedirects(HTTPRedirectHandler):
    """Remote execution never follows an endpoint redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteTransportFailure(
            "REDIRECT_REFUSED",
            f"remote endpoint returned HTTP redirect {code}")


class RemoteTransport(Protocol):
    def send(self, envelope: RemoteEnvelope) -> Any: ...

    def cancel(self, envelope: RemoteEnvelope) -> RemoteCancellation: ...


def serialize_transport_body(envelope: RemoteEnvelope) -> dict[str, Any]:
    """The exact body sent over HTTP; it contains no credentials."""
    return envelope.to_dict()


class UrllibJsonTransport:
    """Synchronous HTTP JSON request/response using only Python stdlib.

    HTTPS uses the platform default certificate-verifying context. Credentials
    are supplied by an explicit external callback and never enter the Tabularium
    envelope, ledger, or trace.
    """

    def __init__(self, *,
                 credential_provider: Callable[[], Mapping[str, str]] | None = None,
                 ssl_context: ssl.SSLContext | None = None):
        if ssl_context is not None:
            from .tls import validate_tls_context
            validate_tls_context(ssl_context)
        self.credential_provider = credential_provider
        self.ssl_context = ssl_context
        self.last_transport_body: dict[str, Any] | None = None
        self.last_response_bytes = 0
        self.last_serialization_ms = 0.0
        self.last_transport_ms = 0.0
        self.last_remote_fixture_ms: float | None = None

    def send(self, envelope: RemoteEnvelope) -> dict[str, Any]:
        body = serialize_transport_body(envelope)
        self.last_transport_body = body
        serialization_started = time.perf_counter()
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        self.last_serialization_ms = round(
            (time.perf_counter() - serialization_started) * 1000, 3)
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json",
                   "X-Tabularium-Request-ID": envelope.interop_request_id,
                   "Idempotency-Key": envelope.idempotency_key}
        if self.credential_provider is not None:
            supplied = self.credential_provider()
            if not isinstance(supplied, Mapping):
                raise RemoteTransportFailure("PROTOCOL_ERROR",
                                              "credential provider did not return a mapping")
            headers.update({str(key): str(value) for key, value in supplied.items()})
        request = Request(
            envelope.interop_request.worker_target.endpoint,
            data=encoded, headers=headers, method="POST")
        if request.full_url.lower().startswith("https://"):
            context = self.ssl_context if self.ssl_context is not None else ssl.create_default_context()
        else:
            context = None
        opener_handlers = [_RefuseRedirects()]
        if context is not None:
            opener_handlers.append(HTTPSHandler(context=context))
        opener = build_opener(*opener_handlers)
        transport_started = time.perf_counter()
        try:
            with opener.open(request, timeout=envelope.interop_request.timeout_s) as response:
                fixture_ms = response.headers.get("X-Tabularium-Fixture-Ms")
                self.last_remote_fixture_ms = float(fixture_ms) if fixture_ms else None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                self.last_response_bytes = len(raw)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RemoteTransportFailure("MALFORMED_RESPONSE",
                                                  "remote response exceeded size limit")
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RemoteTransportFailure("MALFORMED_RESPONSE",
                                                  "remote response was not UTF-8 JSON") from exc
                if not isinstance(value, dict):
                    raise RemoteTransportFailure("MALFORMED_RESPONSE",
                                                  "remote response root was not a mapping")
                return value
        except RemoteTransportFailure:
            raise
        except TimeoutError as exc:
            raise RemoteTransportFailure("TIMEOUT", str(exc) or "remote timeout") from exc
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise RemoteTransportFailure(
                    "REDIRECT_REFUSED",
                    f"remote endpoint returned HTTP redirect {exc.code}") from exc
            raise RemoteTransportFailure("UNAVAILABLE",
                                          f"remote HTTP status {exc.code}") from exc
        except (URLError, OSError) as exc:
            raise RemoteTransportFailure("UNAVAILABLE", str(exc)) from exc
        finally:
            self.last_transport_ms = round(
                (time.perf_counter() - transport_started) * 1000, 3)

    def cancel(self, envelope: RemoteEnvelope) -> RemoteCancellation:
        # The generic endpoint has no cancellation protocol. We do not invent
        # one or claim the remote stopped; callers receive UNKNOWN.
        return RemoteCancellation(
            local_requested=True, remote_request_sent=False,
            remote_confirmed=False, remote_unknown=True,
            detail="HTTP JSON transport has no configured cancellation endpoint")

    def transport_metrics(self, envelope: RemoteEnvelope) -> dict[str, Any]:
        body = self.last_transport_body or serialize_transport_body(envelope)
        return {
            "interop_payload": measure(envelope.interop_request.to_dict()),
            "transport_body": measure(body),
            "response_bytes": self.last_response_bytes,
            "serialization_ms": self.last_serialization_ms,
            "transport_ms": self.last_transport_ms,
            "remote_fixture_ms": self.last_remote_fixture_ms,
        }
