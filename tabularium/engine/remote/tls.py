"""Strict PKI and TLS context management for remote transports (DRL-020)."""

from __future__ import annotations

import ssl
from pathlib import Path


class TLSConfigurationError(ValueError):
    """Raised when TLS or PKI configuration is invalid or insecure."""


def _too_weak(version: ssl.TLSVersion) -> bool:
    """True if the TLS version is weaker than TLSv1.2.

    Handles ssl.TLSVersion sentinel values correctly:
    - MAXIMUM_SUPPORTED (-1): strictly stronger, never weaker
    - MINIMUM_SUPPORTED (-2): explicitly the weakest available
    """
    if version == ssl.TLSVersion.MAXIMUM_SUPPORTED:
        return False
    if version == ssl.TLSVersion.MINIMUM_SUPPORTED:
        return True
    return version < ssl.TLSVersion.TLSv1_2


def build_tls_context(
    *,
    ca_file: str | Path | None = None,
    ca_path: str | Path | None = None,
    cert_file: str | Path | None = None,
    key_file: str | Path | None = None,
    key_password: str | None = None,
    min_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2,
    require_client_cert: bool = False,
) -> ssl.SSLContext:
    """Create a strictly verifying SSLContext.

    In accordance with Tabularium security principles:
    1. Verification is ALWAYS enforced (CERT_REQUIRED).
    2. Hostname checking is ALWAYS enforced.
    3. Weak TLS protocols (< TLSv1_2) are NEVER allowed.
    4. Custom private CA or mTLS client certificates can be configured without
       storing credentials in envelopes or logs.
    """
    if ca_file is not None:
        ca_file_path = Path(ca_file)
        if not ca_file_path.is_file():
            raise TLSConfigurationError(f"CA file does not exist: {ca_file}")
        ca_file_str = str(ca_file_path)
    else:
        ca_file_str = None

    if ca_path is not None:
        ca_dir_path = Path(ca_path)
        if not ca_dir_path.is_dir():
            raise TLSConfigurationError(f"CA directory does not exist: {ca_path}")
        ca_path_str = str(ca_dir_path)
    else:
        ca_path_str = None

    try:
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=ca_file_str,
            capath=ca_path_str,
        )
    except Exception as exc:
        raise TLSConfigurationError(f"Failed to create default SSL context: {exc}") from exc

    # Enforce strict verification and hostname checking
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    # Prevent insecure TLS versions
    if _too_weak(min_version):
        raise TLSConfigurationError("TLS version below TLSv1_2 is strictly prohibited")
    context.minimum_version = min_version

    # Optional mTLS client certificate configuration
    if cert_file is not None:
        cert_path = Path(cert_file)
        if not cert_path.is_file():
            raise TLSConfigurationError(f"Client certificate file does not exist: {cert_file}")
        if key_file is not None:
            key_path = Path(key_file)
            if not key_path.is_file():
                raise TLSConfigurationError(f"Client key file does not exist: {key_file}")
            key_file_str = str(key_path)
        else:
            key_file_str = None

        try:
            context.load_cert_chain(
                certfile=str(cert_path),
                keyfile=key_file_str,
                password=key_password,
            )
        except Exception as exc:
            raise TLSConfigurationError(f"Failed to load client certificate chain: {exc}") from exc
    elif cert_file is None and require_client_cert:
        raise TLSConfigurationError("require_client_cert is True but no cert_file was provided")
    elif key_file is not None:
        raise TLSConfigurationError("key_file provided without cert_file")

    return context


def validate_tls_context(context: ssl.SSLContext | None) -> None:
    """Validate that an external SSLContext complies with strict security standards."""
    if context is None:
        return
    if context.verify_mode != ssl.CERT_REQUIRED:
        raise TLSConfigurationError("SSLContext must require certificate verification (CERT_REQUIRED)")
    if not context.check_hostname:
        raise TLSConfigurationError("SSLContext must enforce hostname checking (check_hostname=True)")
    if _too_weak(context.minimum_version):
        raise TLSConfigurationError("SSLContext minimum TLS version must be at least TLSv1_2")
