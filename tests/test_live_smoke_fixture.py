"""Offline checks for the project used by the live CLI smoke tests."""

from tabularium import change_path
from tabularium.change.preflight import load_change_preflight
from tests.test_live_smoke import _LiveChangeFixture


class TestLiveSmokeFixture(_LiveChangeFixture):
    def test_fixture_passes_change_preflight(self):
        state, refusal = load_change_preflight(
            self.root, allow_untested=False, public_api=change_path,
        )

        self.assertIsNone(refusal)
        base_commit, _, config, _ = state
        self.assertIsNotNone(config)
        self.assertEqual(
            self._git("show", f"{base_commit}:.ai/change.yaml"),
            (self.root / ".ai" / "change.yaml").read_text(encoding="utf-8"),
        )
