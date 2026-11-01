"""Prompt/request parity between the native and API renderers.

Both worker adapters must expose the same governed request semantics to their
execution environment: ``INTENT``, optional ``TASK``, optional ``GOVERNANCE``,
then ``CONTEXT:`` followed by exactly the projected payload they were handed.

This is NOT byte-identity of transport representations: the API worker keeps
its own system message and may carry skill instructions that the native
renderer does not accept. Only the shared semantic prefix and payload are
pinned here, so backend-specific formatting stays free to differ.
"""

from __future__ import annotations

import unittest

from tabularium.engine.workers import WorkerRequest
from tabularium.engine.workers.api.prompt import SYSTEM_INSTRUCTION, render_messages
from tabularium.engine.workers.cli.prompt import render_prompt

CONTEXT_MARKER = "\nCONTEXT:\n"


def request(**overrides) -> WorkerRequest:
    values = dict(
        request_id="parity-1",
        intent="Review the bounded fixture",
        payload={"required": {"task": {"id": "TASK-2026-0001"}},
                 "constraints": ["stay read-only"]},
        task_id="TASK-2026-0001",
        governance={"approval_required": False, "profile": "HOT"},
    )
    values.update(overrides)
    return WorkerRequest(**values)


def split_rendered(text: str) -> tuple[list[str], str]:
    """(semantic prefix lines, payload body) around the CONTEXT marker.

    A trailing newline is transport formatting (the native renderer terminates
    its stdin document); it is stripped so parity covers semantics only.
    """
    head, marker, body = text.partition(CONTEXT_MARKER)
    assert marker, "rendered input is missing the shared CONTEXT: marker"
    return head.splitlines(), body.rstrip("\n")


class PromptParityTests(unittest.TestCase):
    def test_shared_prefix_and_payload_match_without_skill_instructions(self):
        req = request()
        prompt = render_prompt(req)
        user = render_messages(req)[1]
        self.assertEqual(user["role"], "user")
        native_head, native_body = split_rendered(prompt)
        api_head, api_body = split_rendered(user["content"])
        # The full semantic prefix agrees: INTENT first, then TASK/GOVERNANCE.
        self.assertEqual(native_head[0], "INTENT: Review the bounded fixture")
        self.assertEqual(native_head, api_head)
        # The projected payload is rendered identically on both sides.
        self.assertEqual(native_body, api_body)
        self.assertIn("TASK-2026-0001", native_body)

    def test_optional_sections_shrink_identically(self):
        req = request(task_id=None, governance={})
        native_head, _ = split_rendered(render_prompt(req))
        api_head, _ = split_rendered(render_messages(req)[1]["content"])
        self.assertEqual(
            native_head, ["INTENT: Review the bounded fixture"])
        self.assertEqual(native_head, api_head)

    def test_api_only_additions_never_break_native_prefix_parity(self):
        """Skill instructions are an intentional API-side divergence."""
        req = request()
        skill = "Review only the manifest."
        native_head, native_body = split_rendered(render_prompt(req))
        api_head, api_body = split_rendered(
            render_messages(req, skill)[1]["content"])
        self.assertEqual(api_head[:len(native_head)], native_head)
        self.assertGreater(len(api_head), len(native_head))
        self.assertEqual(native_body, api_body)

    def test_system_message_is_api_side_only_and_confers_no_authority(self):
        req = request()
        messages = render_messages(req)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], SYSTEM_INSTRUCTION)
        # The native prompt carries no authority-conferring wrapper either;
        # parity means neither side adds instructions beyond the shared prefix.
        self.assertNotIn(SYSTEM_INSTRUCTION, render_prompt(req))

    def test_payload_omissions_stay_omitted_on_both_sides(self):
        req = request(payload={"required": {"task": {"id": "T1"}}})
        _, native_body = split_rendered(render_prompt(req))
        _, api_body = split_rendered(render_messages(req)[1]["content"])
        for rendered in (native_body, api_body):
            self.assertNotIn("constraints", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
