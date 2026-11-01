"""Acceptance tests for symlink-race-resistant dir_fd writes (M4.4).

Verifies that persistence writes (single records and onboarding foundation batches)
traverse directories via file descriptors opened with O_DIRECTORY | O_NOFOLLOW,
preventing symlink replacement attacks between gate evaluation and write commit.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.persistence import gate as G
from tabularium.engine.persistence import model as M
from tabularium.engine.persistence import writer as W
from tests.test_context_loader import Fixture

REPORT_CONTENT = (
    "---\n"
    'schema_version: "1.0"\n'
    "id: RPT-2026-0045\n"
    "title: Dirfd Race Hardening Verification\n"
    "task: TASK-2026-0045\n"
    'created_at: "2026-10-30T12:00:00Z"\n'
    "created_by: ai:claude\n"
    "---\n"
    "# Report\n\n## Outcome\n\nHardening verified.\n\n"
    "## Validation\n\nAutomated tests.\n\n"
    "## Limitations\n\nNone.\n"
)


class TestWriterDirFdAcceptance(unittest.TestCase):
    """Real filesystem acceptance tests for race-resistant dir_fd writes."""

    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.task("TASK-2026-0045")
        for name in M.FAMILY_DIRECTORY.values():
            (self.fx.project / ".ai" / name).mkdir(parents=True, exist_ok=True)

        self.outside_dir = tempfile.TemporaryDirectory(prefix="tabu-outside-")
        self.addCleanup(self.outside_dir.cleanup)
        self.outside_path = Path(self.outside_dir.name).resolve()

    def _make_report_proposal(self) -> M.PersistenceProposal:
        return M.PersistenceProposal(
            proposal_id="prop-45",
            execution_id="exec-45",
            target_family="Report",
            operation=M.CREATE,
            target_path=".ai/reports/RPT-2026-0045.md",
            content=REPORT_CONTENT,
            record_id="RPT-2026-0045",
            task_id="TASK-2026-0045",
            reason="Verify dir_fd symlink race resistance",
            effective_persistence=M.PROPOSE,
        )

    def _make_authorization(self, proposal: M.PersistenceProposal) -> M.PersistenceAuthorization:
        return M.PersistenceAuthorization(
            proposal_id=proposal.proposal_id,
            execution_id=proposal.execution_id,
            target_path=proposal.target_path,
            operation=proposal.operation,
            content_digest=proposal.content_digest,
            authorized_by="human:user",
            authorized_at="2026-10-30T12:05:00Z",
            binding=proposal.binding(),
        )

    # -------------------------------------------------------------------------
    # Acceptance Test 1: Race, single record
    # -------------------------------------------------------------------------

    def test_acceptance_1_race_single_record_reports_symlink(self) -> None:
        """Acceptance Test 1A: .ai/reports is symlinked outside project after check.

        The race is established via a patch wrapping _open_dir.
        Result must be rejected (WRITE_FAILED) and no file written outside project.
        """
        proposal = self._make_report_proposal()
        auth = self._make_authorization(proposal)
        reports_dir = self.fx.project / ".ai" / "reports"
        real_open = W._open_dir

        def racing_open(root, parts, *args, **kwargs):
            # The swap lands after the gate, right before the directories are opened.
            shutil.rmtree(reports_dir)
            os.symlink(self.outside_path, reports_dir)
            return real_open(root, parts, *args, **kwargs)

        with patch.object(W, "_open_dir", side_effect=racing_open):
            result = W.apply_authorized_proposal(proposal, str(self.fx.project), auth)

        # 1. No files created outside project
        self.assertEqual(os.listdir(self.outside_path), [], "No file may be written outside the project")
        # 2. Result is rejected
        self.assertEqual(result.status, M.FAILED)
        self.assertIn(M.WRITE_FAILED, result.reason_codes)

    def test_acceptance_1_race_single_record_ai_root_symlink(self) -> None:
        """Acceptance Test 1B: .ai is symlinked outside project after check.

        The race is established via a patch wrapping _open_dir.
        Result must be rejected (WRITE_FAILED) and no file written outside project.
        """
        proposal = self._make_report_proposal()
        auth = self._make_authorization(proposal)
        ai_dir = self.fx.project / ".ai"
        real_open = W._open_dir

        def racing_open(root, parts, *args, **kwargs):
            shutil.rmtree(ai_dir)
            os.symlink(self.outside_path, ai_dir)
            return real_open(root, parts, *args, **kwargs)

        with patch.object(W, "_open_dir", side_effect=racing_open):
            result = W.apply_authorized_proposal(proposal, str(self.fx.project), auth)

        self.assertEqual(os.listdir(self.outside_path), [], "No file may be written outside the project")
        self.assertEqual(result.status, M.FAILED)
        self.assertIn(M.WRITE_FAILED, result.reason_codes)

    # -------------------------------------------------------------------------
    # Acceptance Test 2: Race, onboarding batch
    # -------------------------------------------------------------------------

    def test_acceptance_2_race_onboarding_batch(self) -> None:
        """Acceptance Test 2: .ai is symlinked after check during onboarding batch.

        No files created outside project; all-or-nothing preserved.
        """
        holder = tempfile.TemporaryDirectory(prefix="tabu-onboard-proj-")
        self.addCleanup(holder.cleanup)
        project_root = Path(holder.name).resolve()
        initialize(project_root)

        decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": "pytest -q"}
        draft = onboard_project(project_root, decl)
        auths = build_authorizations(draft.proposal, "human:user", "2026-10-30T12:00:00Z")

        ai_dir = project_root / ".ai"
        real_open = W._open_dir

        def racing_open(root, parts, *args, **kwargs):
            # The swap lands after the preflight, right before .ai is opened.
            if parts == (".ai",):
                shutil.rmtree(ai_dir)
                os.symlink(self.outside_path, ai_dir)
            return real_open(root, parts, *args, **kwargs)

        with patch.object(W, "_open_dir", side_effect=racing_open):
            result = onboard_project(project_root, decl, auths)

        # No files created outside project
        self.assertEqual(os.listdir(self.outside_path), [], "No file may be written outside the project")
        # All-or-nothing: operation rejected
        self.assertNotEqual(result.status, "ONBOARDED")

    # -------------------------------------------------------------------------
    # Acceptance Test 3: Temporary file (ENOSPC simulation)
    # -------------------------------------------------------------------------

    def test_acceptance_3_no_temp_files_on_enospc(self) -> None:
        """Acceptance Test 3: In error condition (ENOSPC), no .tabu-*.tmp remains in target dir."""
        proposal = self._make_report_proposal()
        auth = self._make_authorization(proposal)
        reports_dir = self.fx.project / ".ai" / "reports"

        # Simulate ENOSPC on fsync
        real_fsync = os.fsync

        def failing_fsync(fd):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch.object(os, "fsync", side_effect=failing_fsync):
            result = W.apply_authorized_proposal(proposal, str(self.fx.project), auth)

        self.assertEqual(result.status, M.FAILED)
        tmp_files = [f for f in os.listdir(reports_dir) if f.startswith(".tabu-") and f.endswith(".tmp")]
        self.assertEqual(tmp_files, [], "No temporary file may remain in the target directory")

    def test_acceptance_3_exclusive_create_no_temp_files_on_failure(self) -> None:
        """Acceptance Test 3B: If error occurs during _exclusive_create, no temp file remains."""
        ai_dir = self.fx.project / ".ai"
        target_file = ai_dir / "change.yaml"

        def failing_fsync(fd):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch.object(os, "fsync", side_effect=failing_fsync):
            with self.assertRaises(OSError):
                W._exclusive_create(target_file, "test_command: [pytest]\n")

        tmp_files = [f for f in os.listdir(ai_dir) if f.startswith(".tabu-") and f.endswith(".tmp")]
        self.assertEqual(tmp_files, [], "No temporary file may remain in the target directory")

    # -------------------------------------------------------------------------
    # Acceptance Test 5: Platform support (patch as if dir_fd does not exist)
    # -------------------------------------------------------------------------

    def test_acceptance_5_platform_fallback_without_dir_fd(self) -> None:
        """Acceptance Test 5: When dir_fd is simulated as unavailable, legacy fallback works and tests pass."""
        with patch.object(W, "_supports_dir_fd", return_value=False):
            # 1. Single record write
            proposal = self._make_report_proposal()
            auth = self._make_authorization(proposal)
            result = W.apply_authorized_proposal(proposal, str(self.fx.project), auth)
            self.assertEqual(result.status, M.WRITTEN)
            target = self.fx.project / ".ai" / "reports" / "RPT-2026-0045.md"
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), REPORT_CONTENT)

            # 2. Onboarding batch
            holder = tempfile.TemporaryDirectory(prefix="tabu-fallback-proj-")
            self.addCleanup(holder.cleanup)
            project_root = Path(holder.name).resolve()
            initialize(project_root)
            decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": "pytest -q"}
            draft = onboard_project(project_root, decl)
            auths = build_authorizations(draft.proposal, "human:user", "2026-10-30T12:00:00Z")
            res = onboard_project(project_root, decl, auths)
            self.assertEqual(res.status, "ONBOARDED")
            self.assertTrue((project_root / ".ai" / "change.yaml").is_file())

            # 3. ENOSPC cleanup also works on fallback path
            content_46 = REPORT_CONTENT.replace("RPT-2026-0045", "RPT-2026-0046")
            def failing_fsync(fd):
                raise OSError(errno.ENOSPC, "No space left on device")

            with patch.object(os, "fsync", side_effect=failing_fsync):
                prop2 = M.PersistenceProposal(
                    proposal_id="prop-45b",
                    execution_id="exec-45",
                    target_family="Report",
                    operation=M.CREATE,
                    target_path=".ai/reports/RPT-2026-0046.md",
                    content=content_46,
                    record_id="RPT-2026-0046",
                    task_id="TASK-2026-0045",
                    reason="Verify fallback cleanup",
                    effective_persistence=M.PROPOSE,
                )
                auth2 = self._make_authorization(prop2)
                res_fail = W.apply_authorized_proposal(prop2, str(self.fx.project), auth2)
                self.assertEqual(res_fail.status, M.FAILED)
                tmp_files = [f for f in os.listdir(self.fx.project / ".ai" / "reports") if f.startswith(".tabu-") and f.endswith(".tmp")]
                self.assertEqual(tmp_files, [])


class TestBatchHoldsOneDirectory(unittest.TestCase):
    """One .ai descriptor held for the entire batch write."""

    def test_swap_in_the_middle_of_the_batch_does_not_split_it(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="tabu-midbatch-")
        self.addCleanup(holder.cleanup)
        root = Path(holder.name).resolve()
        initialize(root)
        outside = Path(tempfile.mkdtemp(prefix="tabu-midbatch-out-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "marker").write_text("x")
        decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": "pytest -q"}
        draft = onboard_project(root, decl)
        auths = build_authorizations(draft.proposal, "human:user", "2026-10-30T12:00:00Z")
        real_create = W._fd_create
        moved = root / ".ai-moved"

        def create_then_swap(dir_fd, name, content):
            real_create(dir_fd, name, content)          # change.yaml, the first member
            (root / ".ai").rename(moved)
            os.symlink(outside, root / ".ai")

        with patch.object(W, "_fd_create", side_effect=create_then_swap):
            result = onboard_project(root, decl, auths)
        self.assertEqual(sorted(os.listdir(outside)), ["marker"], "nothing written outside")
        # The held descriptor no longer matches root/.ai after the swap: the
        # writes in the moved directory are rolled back and the batch refused.
        self.assertFalse((moved / "change.yaml").exists(), "created member rolled back")
        self.assertNotEqual(result.status, "ONBOARDED")


class TestRootIdentity(TestWriterDirFdAcceptance):
    """The project root itself is swapped during writing."""

    def test_root_swapped_for_a_symlink_writes_nothing_outside(self) -> None:
        proposal = self._make_report_proposal()
        auth = self._make_authorization(proposal)
        project = Path(self.fx.project).resolve()
        outside = Path(tempfile.mkdtemp(prefix="tabu-root-swap-out-"))
        self.addCleanup(shutil.rmtree, outside, True)
        moved = project.with_name(project.name + "-moved")
        real_open = W._open_dir

        def swap_root(root, parts, *args, **kwargs):
            # Replace the whole project with a symlink to a look-alike tree.
            shutil.copytree(project, outside / "tree", symlinks=True)
            project.rename(moved)
            os.symlink(outside / "tree", project)
            return real_open(root, parts, *args, **kwargs)

        try:
            with patch.object(W, "_open_dir", side_effect=swap_root):
                result = W.apply_authorized_proposal(proposal, str(project), auth)
        finally:
            if project.is_symlink():
                project.unlink()
                moved.rename(project)
        self.assertNotEqual(result.status, M.WRITTEN)
        written = [p for p in (outside / "tree").rglob("*") if p.name.startswith("RPT-")]
        self.assertEqual(written, [], "nothing written into the look-alike tree")


    def test_ancestor_swap_is_caught_by_the_root_identity(self) -> None:
        # O_NOFOLLOW only guards the last component of the root path; a
        # swapped ancestor with a look-alike project inside is caught by the
        # (st_dev, st_ino) check alone.
        proposal = self._make_report_proposal()
        auth = self._make_authorization(proposal)
        project = Path(self.fx.project).resolve()
        parent = project.parent
        # The test renames this directory: it must be the fixture's own, never
        # the shared temp root.
        self.assertNotEqual(parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(parent.name.startswith("ctxload-"), parent)
        holder = Path(tempfile.mkdtemp(prefix="tabu-ancestor-"))
        self.addCleanup(shutil.rmtree, holder, True)
        moved = holder / "moved-parent"
        lookalike = holder / "lookalike"
        real_open = W._open_dir

        def swap_ancestor(root, parts, *args, **kwargs):
            shutil.copytree(parent, lookalike, symlinks=True)
            parent.rename(moved)
            os.symlink(lookalike, parent)
            return real_open(root, parts, *args, **kwargs)

        try:
            with patch.object(W, "_open_dir", side_effect=swap_ancestor):
                result = W.apply_authorized_proposal(proposal, str(project), auth)
        finally:
            if parent.is_symlink():
                parent.unlink()
                moved.rename(parent)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertEqual([p for p in lookalike.rglob("RPT-*")], [])

    def test_single_record_ai_symlink_is_caught_by_o_nofollow(self) -> None:
        # The single-record path checks only the root identity; a .ai symlink
        # to a directory that does contain reports/ is refused by O_NOFOLLOW.
        proposal = self._make_report_proposal()
        auth = self._make_authorization(proposal)
        project = Path(self.fx.project).resolve()
        outside = Path(tempfile.mkdtemp(prefix="tabu-ai-link-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "reports").mkdir()
        real_open = W._open_dir

        def swap_ai(root, parts, *args, **kwargs):
            (project / ".ai").rename(project / ".ai-real")
            os.symlink(outside, project / ".ai")
            return real_open(root, parts, *args, **kwargs)

        with patch.object(W, "_open_dir", side_effect=swap_ai):
            result = W.apply_authorized_proposal(proposal, str(project), auth)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertEqual(list((outside / "reports").iterdir()), [])


class TestDirFdSymlinkAndRecordModes(unittest.TestCase):
    """In-project symlinks, record permissions, identity checks, and atomic writes."""

    def setUp(self) -> None:
        base = TestWriterDirFdAcceptance("setUp")
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.base = base
        self.project = Path(base.fx.project).resolve()
        self.proposal = base._make_report_proposal()
        self.auth = base._make_authorization(self.proposal)

    def _race(self, swap):
        real_open = W._open_dir

        def racing_open(root, parts, *args, **kwargs):
            swap()
            return real_open(root, parts, *args, **kwargs)

        with patch.object(W, "_open_dir", side_effect=racing_open):
            return W.apply_authorized_proposal(self.proposal, str(self.project), self.auth)

    def test_b5_written_record_is_0600(self) -> None:
        import stat as _stat
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        result = W.apply_authorized_proposal(self.proposal, str(self.project), self.auth)
        self.assertEqual(result.status, M.WRITTEN)
        mode = _stat.S_IMODE((self.project / self.proposal.target_path).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_b1_in_project_dir_symlink_is_not_followed(self) -> None:
        reports = self.project / ".ai" / "reports"
        tasks = self.project / ".ai" / "tasks"
        before = sorted(p.name for p in tasks.iterdir())

        def swap() -> None:
            shutil.rmtree(reports)
            os.symlink(tasks, reports)       # stays inside the project

        result = self._race(swap)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertEqual(sorted(p.name for p in tasks.iterdir()), before)

    def test_b3_final_entry_symlink_is_not_written_through(self) -> None:
        victim = self.project / ".ai" / "victim.md"
        victim.write_text("VICTIM\n")
        target = self.project / self.proposal.target_path

        def swap() -> None:
            os.symlink("../victim.md", target)

        result = self._race(swap)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertEqual(victim.read_text(), "VICTIM\n")

    def test_b4_unidentifiable_root_refuses(self) -> None:
        with patch.object(W, "_identity", side_effect=OSError(2, "gone")):
            result = W.apply_authorized_proposal(self.proposal, str(self.project), self.auth)
        self.assertEqual(result.status, M.FAILED)
        self.assertFalse((self.project / self.proposal.target_path).exists())


class TestDirFdBatchAtomicSwaps(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="tabu-dirfd-batch-")
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()
        initialize(self.root)
        self.decl = {"title": "T", "purpose": "P", "scope": "S", "test_command": "pytest -q"}

    def test_b3_batch_foundation_symlink_is_refused(self) -> None:
        ai = self.root / ".ai"
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        (ai / "project.md").rename(ai / "project.md.bak")
        os.symlink("project.md.bak", ai / "project.md")
        before = (ai / "project.md.bak").read_text()
        result = onboard_project(self.root, self.decl, auths)
        self.assertNotEqual(result.status, "ONBOARDED")
        self.assertEqual((ai / "project.md.bak").read_text(), before)

    def test_b2_ai_replaced_by_a_real_directory_mid_batch_is_rolled_back(self) -> None:
        draft = onboard_project(self.root, self.decl)
        auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        fresh = Path(tempfile.mkdtemp(prefix="tabu-dirfd-fresh-"))
        self.addCleanup(shutil.rmtree, fresh, True)
        initialize(fresh)
        moved = self.root / ".ai-moved"
        real_create = W._fd_create

        def create_then_replace(dir_fd, name, content):
            real_create(dir_fd, name, content)
            (self.root / ".ai").rename(moved)
            shutil.copytree(fresh / ".ai", self.root / ".ai")   # a valid-looking scaffold

        with patch.object(W, "_fd_create", side_effect=create_then_replace):
            result = onboard_project(self.root, self.decl, auths)
        self.assertNotEqual(result.status, "ONBOARDED", "no false success")
        self.assertFalse((moved / "change.yaml").exists(), "writes in the moved directory rolled back")
        self.assertFalse((self.root / ".ai" / "change.yaml").exists())


class TestEachLayerAlone(unittest.TestCase):
    """Each defense layer measured where no other layer can mask it."""

    def setUp(self) -> None:
        base = TestWriterDirFdAcceptance("setUp")
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.project = Path(base.fx.project).resolve()
        self.proposal = base._make_report_proposal()
        self.auth = base._make_authorization(self.proposal)

    def test_b1_dir_symlink_before_the_relative_path_is_taken(self) -> None:
        # Swap before _relative_dir: resolve() there would follow it into tasks/.
        reports = self.project / ".ai" / "reports"
        tasks = self.project / ".ai" / "tasks"
        before = sorted(p.name for p in tasks.iterdir())
        real_rel = W._relative_dir

        def swap_then_rel(root, target):
            shutil.rmtree(reports)
            os.symlink(tasks, reports)
            return real_rel(root, target)

        with patch.object(W, "_relative_dir", side_effect=swap_then_rel):
            result = W.apply_authorized_proposal(self.proposal, str(self.project), self.auth)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertEqual(sorted(p.name for p in tasks.iterdir()), before)

    def test_create_is_exclusive_against_a_regular_file(self) -> None:
        # A plain file (not a symlink) appearing after the gate: only the
        # exclusive create refuses it; a replace would overwrite it.
        target = self.project / self.proposal.target_path
        real_open = W._open_dir

        def appear(root, parts, *args, **kwargs):
            target.write_text("SOMEONE ELSE\n")
            return real_open(root, parts, *args, **kwargs)

        with patch.object(W, "_open_dir", side_effect=appear):
            result = W.apply_authorized_proposal(self.proposal, str(self.project), self.auth)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertEqual(target.read_text(), "SOMEONE ELSE\n")


class TestWriterBatchDirect(unittest.TestCase):
    """The writer called directly, without onboarding's own .ai classification."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="tabu-batch-direct-")
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()
        initialize(self.root)
        decl = {"title": "T", "purpose": "P", "scope": "S"}
        draft = onboard_project(self.root, decl)
        from tabularium.onboarding import FoundationProposalBatch
        self.batch = FoundationProposalBatch(
            proposal_id=draft.proposal.operation_id,
            onboarding_operation_id=draft.proposal.operation_id,
            project_root=str(self.root), proposals=draft.proposal.durable_proposals)
        self.auths = build_authorizations(draft.proposal, "k", "2026-10-30T00:00:00Z")
        self.ai = self.root / ".ai"

    def test_lexical_symlink_foundation_is_refused_by_the_writer(self) -> None:
        (self.ai / "project.md").rename(self.ai / "project.md.bak")
        os.symlink("project.md.bak", self.ai / "project.md")
        before = (self.ai / "project.md.bak").read_text()
        result = W.apply_authorized_proposal(self.batch, str(self.root), self.auths)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertEqual((self.ai / "project.md.bak").read_text(), before)

    def test_final_entry_turned_symlink_after_preflight_is_not_replaced(self) -> None:
        bak = self.ai / "project.md.bak"
        real_open = W._open_dir

        def swap(root, parts, *args, **kwargs):
            (self.ai / "project.md").rename(bak)
            os.symlink("project.md.bak", self.ai / "project.md")
            return real_open(root, parts, *args, **kwargs)

        before = (self.ai / "project.md").read_text()
        with patch.object(W, "_open_dir", side_effect=swap):
            result = W.apply_authorized_proposal(self.batch, str(self.root), self.auths)
        self.assertNotEqual(result.status, M.WRITTEN)
        self.assertTrue((self.ai / "project.md").is_symlink(), "the symlink was not replaced")
        self.assertEqual(bak.read_text(), before)
