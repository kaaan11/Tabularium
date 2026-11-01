"""Baseline conformance for persistence gate condition 6.

Pins the ratified clean-baseline semantics (DEC draft A): condition 6 admits exactly
ONE mechanism — an explicit, content-bound `PersistenceAuthorization`.
Workflow-derived or Task-scope-derived substitutes do not exist; anything
that is not such an authorization fails closed with
PERSISTENCE_AUTHORIZATION_REQUIRED.
"""

import unittest

from tabularium.engine.persistence import gate as G
from tabularium.engine.persistence.model import (
    PERSISTENCE_AUTHORIZATION_REQUIRED,
    PROPOSE,
    PersistenceAuthorization,
    PersistenceProposal,
)

WHEN = "2026-08-22T00:00:00+00:00"
ACTOR = "alex"


def _proposal():
    return PersistenceProposal(
        proposal_id="P-CONformance-1",
        execution_id="EXEC-CONFORMANCE-1",
        target_family="Report",
        operation="CREATE",
        target_path=".ai/reports/RPT-2026-0001.md",
        content="---\nid: RPT-2026-0001\nschema_version: \"1.0\"\n---\n\nbody",
        record_id="RPT-2026-0001",
        reason="condition 6 baseline conformance",
        effective_persistence=PROPOSE,
    )


def _authorization(proposal, binding=None):
    return PersistenceAuthorization(
        proposal_id=proposal.proposal_id,
        execution_id=proposal.execution_id,
        target_path=proposal.target_path,
        operation=proposal.operation,
        content_digest=proposal.content_digest,
        authorized_by=ACTOR,
        authorized_at=WHEN,
        binding=(proposal.binding() if binding is None else binding),
        note="baseline conformance",
    )


class TestCondition6SinglePath(unittest.TestCase):
    """Condition 6 has exactly one satisfied shape: the authorization object."""

    def evaluate_with(self, authorization):
        # The project root is irrelevant here: other conditions may fail on
        # it, but every assertion below targets the condition-6 reason code.
        return G.evaluate(_proposal(), "/nonexistent-project", authorization)

    def test_missing_authorization_denied(self):
        result = self.evaluate_with(None)
        self.assertFalse(result.eligible)
        self.assertIn(PERSISTENCE_AUTHORIZATION_REQUIRED, result.reason_codes)

    def test_boolean_is_not_an_authorization(self):
        result = self.evaluate_with(True)
        self.assertIn(PERSISTENCE_AUTHORIZATION_REQUIRED, result.reason_codes)

    def test_approval_word_is_not_an_authorization(self):
        result = self.evaluate_with("APPROVED")
        self.assertIn(PERSISTENCE_AUTHORIZATION_REQUIRED, result.reason_codes)

    def test_workflow_scope_claim_dict_is_not_an_authorization(self):
        # A workflow/Task-scope style claim carries no content binding and is
        # structurally incapable of satisfying condition 6 in this baseline.
        claim = {"approved_workflow": "WF-1", "task_scope": ["reports"],
                 "authorizes_exact_write": True}
        result = self.evaluate_with(claim)
        self.assertFalse(result.eligible)
        self.assertIn(PERSISTENCE_AUTHORIZATION_REQUIRED, result.reason_codes)

    def test_execution_resolution_shape_is_not_an_authorization(self):
        resolution = {"approval_request_id": "r", "execution_id": "e",
                      "decision": "APPROVE", "decided_by": ACTOR,
                      "decided_at": WHEN}
        result = self.evaluate_with(resolution)
        self.assertIn(PERSISTENCE_AUTHORIZATION_REQUIRED, result.reason_codes)

    def test_valid_authorization_object_round_trips_and_binds(self):
        proposal = _proposal()
        auth = _authorization(proposal)
        parsed = PersistenceAuthorization.from_dict(auth.to_dict())
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.content_digest, proposal.content_digest)
        self.assertEqual(parsed.binding, proposal.binding())

    def test_condition_six_identity_unchanged(self):
        self.assertEqual(G.CONDITION_NAMES[5], (6, "PERSISTENCE_AUTHORIZED"))

    # --- exact binding-field semantics (AUD-02 alignment cases) -----------
    #
    # Runtime truth: the composite `binding` digest is OPTIONAL. The five
    # field matches (proposal id, execution id, target path, operation,
    # content digest) are what make authorization content-bound; a present
    # binding must equal the canonical one, an absent/empty one is simply
    # not checked. These tests pin that precisely — no stronger guarantee.
    #
    # Three distinct cases are kept deliberately separate and must not be
    # conflated:
    #   1. binding KEY ABSENT from the serialized payload
    #      -> test_absent_binding_key_passes
    #   2. binding key present with an EMPTY VALUE at construction time
    #      -> test_empty_binding_is_not_checked
    #   3. binding parsed non-empty, then emptied in memory
    #      -> test_empty_internal_binding_on_parsed_object_is_not_checked

    def _assert_no_authorization_failure(self, result):
        self.assertNotIn(PERSISTENCE_AUTHORIZATION_REQUIRED,
                         result.reason_codes)
        self.assertNotIn("PERSISTENCE_AUTHORIZATION_MISMATCH",
                         result.reason_codes)

    def test_empty_internal_binding_on_parsed_object_is_not_checked(self):
        # Distinct from `test_absent_binding_key_passes`: here the serialized
        # payload DOES carry the canonical binding key, the object is parsed
        # normally, and only its in-memory binding field is then emptied.
        # This pins the parsed-object path (as opposed to the constructor
        # path covered by `test_empty_binding_is_not_checked`): condition 6
        # reads the live field, so an empty internal representation is not
        # checked either.
        auth = _authorization(_proposal(), binding=None)
        parsed = PersistenceAuthorization.from_dict(auth.to_dict())
        self.assertEqual(parsed.binding, _proposal().binding())
        object.__setattr__(parsed, "binding", "")
        result = self.evaluate_with(parsed)
        self._assert_no_authorization_failure(result)

    def test_absent_binding_key_passes(self):
        # Literal omission case: the serialized authorization carries NO
        # `binding` key at all. from_dict defaults it to "", and condition 6
        # passes on the field matches alone.
        payload = _authorization(_proposal()).to_dict()
        assert "binding" in payload
        del payload["binding"]
        parsed = PersistenceAuthorization.from_dict(payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.binding, "")
        result = self.evaluate_with(parsed)
        self._assert_no_authorization_failure(result)

    def test_empty_binding_is_not_checked(self):
        result = self.evaluate_with(_authorization(_proposal(), binding=""))
        self._assert_no_authorization_failure(result)

    def test_matching_binding_passes(self):
        result = self.evaluate_with(_authorization(_proposal()))
        self._assert_no_authorization_failure(result)

    def test_present_but_wrong_binding_fails_closed(self):
        result = self.evaluate_with(
            _authorization(_proposal(), binding="0" * 64))
        self.assertIn("PERSISTENCE_AUTHORIZATION_MISMATCH", result.reason_codes)
        self.assertFalse(result.eligible)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
