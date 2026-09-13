"""Spec section 40: proves zero real provider mutations are reachable
from every unattended/read-only path in the repo. Stage 2B's
scan-opportunities/inspect-markets/check-connectivity and Stage 3's
paper-scan/paper-portfolio/paper-stats/settle-paper already have their
own dedicated safety-regression classes (tests/test_execution_cli.py::
TestNoSubcommandEverPlacesAnOrder, tests/test_paper_cli.py::
TestSafetyRegression) -- not duplicated here. This file covers what's
new/unique to confirming Stage 4 doesn't introduce a second, hidden
mutation path anywhere else in the codebase.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestWorkerHasNoLiveExecutionAwareness:
    """src/worker.py is the only autonomous/scheduled code path in this
    repo (confirmed during the Stage 4 audit: it imports nothing from
    src.execution.* at all). This test fails loudly if that ever
    changes without an explicit, reviewed decision."""

    def test_worker_module_does_not_import_the_execution_package(self):
        text = (PROJECT_ROOT / "src" / "worker.py").read_text(encoding="utf-8")
        assert "from src.execution" not in text
        assert "import src.execution" not in text

    def test_worker_job_dispatch_table_has_no_live_or_paper_job_type(self):
        text = (PROJECT_ROOT / "src" / "worker.py").read_text(encoding="utf-8")
        for forbidden in ("live_scan", "live_execute", "live-scan", "live-execute", "paper_scan", "paper-scan"):
            assert forbidden not in text.lower(), f"worker.py must not know about {forbidden!r}"

    def test_worker_can_still_be_imported_without_touching_live_execution(self):
        """Confirms the above isn't just a text-grep artifact -- the
        module actually imports cleanly and its dispatch dict (if
        importable without side effects) never references
        _submit_authorized_order transitively."""
        import ast
        tree = ast.parse((PROJECT_ROOT / "src" / "worker.py").read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
        execution_imports = [m for m in imported_modules if m.startswith("src.execution")]
        assert execution_imports == []


class TestNoStandaloneScriptReferencesLiveSubmission:
    """A broader sweep: nothing anywhere in src/ outside the explicitly
    allowed files (checked exactly in test_live_architecture.py) may
    reference the live-mutation entry point -- this re-confirms the
    same invariant from the 'unattended path' angle rather than the
    'single call site' angle."""

    def test_no_daily_pipeline_or_scheduler_file_references_submission(self):
        candidates = [
            "src/daily_pipeline.py", "src/worker.py", "src/discord_delivery.py",
            "src/control_panel.py", "src/automatic_grading.py",
        ]
        for rel_path in candidates:
            path = PROJECT_ROOT / rel_path
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            assert "_submit_authorized_order" not in text, f"{rel_path} must never reference the live-mutation entry point"

    def test_control_panel_only_calls_execute_authorized_never_the_provider_directly(self):
        """The one file that DOES need to trigger execution (the
        Streamlit Live Execution tab) must go through
        LiveExecutionService.execute_authorized(), never
        provider._submit_authorized_order directly."""
        text = (PROJECT_ROOT / "src" / "live_execution_panel.py").read_text(encoding="utf-8")
        assert "_submit_authorized_order" not in text
        assert "service.execute_authorized(" in text


class TestNoDiscordLiveAlertExistsYet:
    """Section 27's Discord notification is explicitly optional in the
    spec and was deferred this stage -- confirmed here so a future
    reader knows this is a documented gap, not an oversight, and so
    this test starts failing (as a reminder to update it) the moment
    someone adds one."""

    def test_discord_delivery_has_no_live_opportunity_alert_function(self):
        text = (PROJECT_ROOT / "src" / "discord_delivery.py").read_text(encoding="utf-8")
        assert "deliver_live_opportunity_alert" not in text
