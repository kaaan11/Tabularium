"""Unit and integration tests for A2A mapping (DRL-019) and PKI/TLS (DRL-020)."""

import ssl
import tempfile
import unittest
from pathlib import Path

from tabularium.engine.interop import (
    A2A_PROTOCOL_VERSION,
    REMOTE_COMPLETED,
    REMOTE_FAILED,
    REMOTE_FAILED_STATUS,
    REMOTE_INPUT_REQUIRED,
    InteropRequest,
    RemoteIdentity,
    deserialize_a2a_task_response,
    serialize_a2a_task_request,
)
from tabularium.engine.interop.model import (
    MALFORMED_RESPONSE,
    PROTOCOL_ERROR,
    WRONG_TASK_ID,
)
from tabularium.engine.projection.model import ProjectedPayload
from tabularium.engine.remote import (
    TLSConfigurationError,
    UrllibJsonTransport,
    build_tls_context,
    validate_tls_context,
)


class TestA2AMapping(unittest.TestCase):
    def setUp(self):
        self.target = RemoteIdentity(
            provider="google",
            model="gemini-2.5-pro",
            worker="remote-agent",
            endpoint="https://remote.example.com/a2a",
            agent_id="agent-007",
        )
        self.payload = ProjectedPayload((
            ("required", {"intent": "investigate security", "task": {"id": "TASK-2026-0001"}}),
            ("constraints", {"text": "read-only boundary"}),
        ))
        self.request = InteropRequest(
            interop_request_id="req-a2a-1",
            execution_id="exec-123",
            task_id="TASK-2026-0001",
            worker_target=self.target,
            projected_payload=self.payload,
            requested_capability="STRUCTURED_OUTPUT",
            metadata={"intent": "investigate security"},
            timeout_s=30.0,
        )

    def test_serialize_a2a_task_request(self):
        data = serialize_a2a_task_request(self.request)
        self.assertEqual(data["protocol_version"], A2A_PROTOCOL_VERSION)
        self.assertEqual(data["task_id"], "TASK-2026-0001")
        self.assertEqual(data["interop_request_id"], "req-a2a-1")
        self.assertEqual(data["target"]["agent_id"], "agent-007")

        message = data["message"]
        self.assertEqual(message["role"], "user")
        self.assertEqual(len(message["parts"]), 3)
        self.assertEqual(message["parts"][0]["type"], "text")
        self.assertIn("investigate security", message["parts"][0]["text"])

        data_part = message["parts"][1]
        self.assertEqual(data_part["type"], "data")
        self.assertEqual(data_part["data"]["section"], "required")

    def test_deserialize_a2a_task_response_completed(self):
        raw_response = {
            "task_id": "TASK-2026-0001",
            "interop_request_id": "req-a2a-1",
            "result_id": "res-999",
            "status": "COMPLETED",
            "message": {
                "role": "assistant",
                "parts": [
                    {"type": "text", "text": "Investigation finished."},
                    {"type": "data", "data": {"finding": "none"}},
                ],
            },
        }
        result = deserialize_a2a_task_response(raw_response, self.request)
        self.assertEqual(result.status, REMOTE_COMPLETED)
        self.assertEqual(result.output, "Investigation finished.")
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(result.task_id, "TASK-2026-0001")

    def test_deserialize_a2a_task_response_input_required(self):
        raw_response = {
            "task_id": "TASK-2026-0001",
            "interop_request_id": "req-a2a-1",
            "status": "INPUT_REQUIRED",
            "output": "Need confirmation",
        }
        result = deserialize_a2a_task_response(raw_response, self.request)
        self.assertEqual(result.status, REMOTE_INPUT_REQUIRED)
        self.assertEqual(result.output, "Need confirmation")

    def test_deserialize_a2a_task_response_malformed(self):
        result = deserialize_a2a_task_response("invalid-json", self.request)
        self.assertEqual(result.status, REMOTE_FAILED_STATUS)
        self.assertIsNotNone(result.error)

    # -- BUG-06 acceptance criteria ------------------------------------------

    def test_empty_response_is_not_completed(self):
        """An empty dict {} must NOT be interpreted as completed."""
        result = deserialize_a2a_task_response({}, self.request)
        self.assertEqual(result.status, REMOTE_FAILED_STATUS)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, MALFORMED_RESPONSE)

    def test_missing_status_is_not_completed(self):
        """A response with body but no status field must fail closed."""
        data = {"task_id": "TASK-2026-0001", "output": "some output"}
        result = deserialize_a2a_task_response(data, self.request)
        self.assertEqual(result.status, REMOTE_FAILED_STATUS)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, MALFORMED_RESPONSE)

    def test_foreign_task_id_is_refused(self):
        """A remote claiming a different task_id must be rejected."""
        data = {"task_id": "TASK-9999", "status": "COMPLETED", "output": "ok"}
        result = deserialize_a2a_task_response(data, self.request)
        self.assertEqual(result.status, REMOTE_FAILED_STATUS)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, WRONG_TASK_ID)

    def test_foreign_interop_request_id_is_refused(self):
        """A remote claiming a different interop_request_id must be rejected."""
        data = {
            "task_id": "TASK-2026-0001",
            "interop_request_id": "req-FOREIGN",
            "status": "COMPLETED",
            "output": "ok",
        }
        result = deserialize_a2a_task_response(data, self.request)
        self.assertEqual(result.status, REMOTE_FAILED_STATUS)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, PROTOCOL_ERROR)

    def test_error_object_forces_failed_status(self):
        """A response with error object must never be COMPLETED."""
        data = {
            "task_id": "TASK-2026-0001",
            "status": "COMPLETED",
            "error": {"code": "INTERNAL", "message": "something broke"},
        }
        result = deserialize_a2a_task_response(data, self.request)
        self.assertEqual(result.status, REMOTE_FAILED_STATUS)
        self.assertIsNotNone(result.error)

    def test_identity_is_never_taken_from_the_remote(self):
        """Result identity fields must come from expected_request, not the remote."""
        data = {
            "task_id": "TASK-2026-0001",
            "interop_request_id": "req-a2a-1",
            "status": "COMPLETED",
            "output": "done",
        }
        result = deserialize_a2a_task_response(data, self.request)
        self.assertEqual(result.task_id, self.request.task_id)
        self.assertEqual(result.interop_request_id, self.request.interop_request_id)
        self.assertEqual(result.execution_id, self.request.execution_id)


    def test_wrong_typed_optional_fields_are_malformed(self):
        """A broken optional field must not read as an absent one."""
        for payload in ({"status": "COMPLETED", "artifacts": "nope"},
                        {"status": "COMPLETED", "message": "nope"},
                        {"status": "COMPLETED", "message": {"parts": "nope"}}):
            with self.subTest(payload=payload):
                result = deserialize_a2a_task_response(payload, self.request)
                self.assertEqual(result.status, REMOTE_FAILED_STATUS)
                self.assertIsNotNone(result.error)
                self.assertEqual(result.error.code, MALFORMED_RESPONSE)
                self.assertEqual(result.task_id, self.request.task_id)


class TestPKITLS(unittest.TestCase):
    def test_build_default_tls_context_is_secure(self):
        ctx = build_tls_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_insecure_min_version_rejected(self):
        with self.assertRaises(TLSConfigurationError):
            build_tls_context(min_version=ssl.TLSVersion.TLSv1)

    def test_nonexistent_ca_rejected(self):
        with self.assertRaises(TLSConfigurationError):
            build_tls_context(ca_file="/nonexistent/ca.pem")

    def test_validate_tls_context(self):
        # Default is valid
        ctx = build_tls_context()
        validate_tls_context(ctx)

        # Insecure context must be rejected
        insecure_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure_ctx.check_hostname = False
        insecure_ctx.verify_mode = ssl.CERT_NONE
        with self.assertRaises(TLSConfigurationError):
            validate_tls_context(insecure_ctx)

    # -- BUG-09 TLS sentinel tests ---

    def test_maximum_supported_sentinel_is_accepted(self):
        """MAXIMUM_SUPPORTED (-1) must not be rejected as too weak."""
        ctx = build_tls_context(min_version=ssl.TLSVersion.MAXIMUM_SUPPORTED)
        self.assertIsNotNone(ctx)

    def test_minimum_supported_sentinel_is_rejected(self):
        """MINIMUM_SUPPORTED (-2) is explicitly the weakest; must be rejected."""
        with self.assertRaises(TLSConfigurationError):
            build_tls_context(min_version=ssl.TLSVersion.MINIMUM_SUPPORTED)

    def test_require_client_cert_without_cert_fails(self):
        """require_client_cert=True without cert_file must raise TLSConfigurationError."""
        with self.assertRaises(TLSConfigurationError):
            build_tls_context(require_client_cert=True)

    def test_urllib_json_transport_with_custom_secure_context(self):
        ctx = build_tls_context()
        transport = UrllibJsonTransport(ssl_context=ctx)
        self.assertIs(transport.ssl_context, ctx)

    def test_urllib_json_transport_rejects_insecure_context(self):
        insecure_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure_ctx.check_hostname = False
        insecure_ctx.verify_mode = ssl.CERT_NONE
        with self.assertRaises(TLSConfigurationError):
            UrllibJsonTransport(ssl_context=insecure_ctx)


if __name__ == "__main__":
    unittest.main()
