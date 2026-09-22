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
    """src/worker.py originally imported nothing from src.execution.*
    at all (Stage 4 audit finding). That changed 2026-09-21 by an
    explicit, reviewed decision -- the user's own "Hybrid: auto-execute
    under a hard dollar cap" choice for the per-customer Polymarket
    Auto-Bet feature -- so the worker now reaches live execution, but
    ONLY indirectly, through the single, fully-gated
    src.execution.customer_autobet.run_customer_autobet_pass entry
    point, which itself only ever calls the unmodified,
    human-approval-shaped LiveExecutionService.execute_authorized()
    (see src/execution/customer_autobet.py's own module docstring and
    tests/test_customer_autobet.py's hybrid-approval coverage). These
    tests lock that boundary down so a FUTURE change can't wire
    worker.py to some other, less-reviewed execution path -- a raw
    live-scan/live-execute/paper-scan job, or a second src.execution
    import -- without this test failing loudly."""

    def test_worker_imports_only_the_customer_autobet_entry_point_from_execution(self):
        text = (PROJECT_ROOT / "src" / "worker.py").read_text(encoding="utf-8")
        execution_import_lines = [
            line.strip() for line in text.splitlines()
            if "from src.execution" in line or "import src.execution" in line
        ]
        assert execution_import_lines == [
            "from src.execution.customer_autobet import run_customer_autobet_pass"
        ], f"worker.py's src.execution import(s) changed unexpectedly: {execution_import_lines}"

    def test_worker_job_dispatch_table_has_no_live_or_paper_job_type(self):
        text = (PROJECT_ROOT / "src" / "worker.py").read_text(encoding="utf-8")
        for forbidden in ("live_scan", "live_execute", "live-scan", "live-execute", "paper_scan", "paper-scan"):
            assert forbidden not in text.lower(), f"worker.py must not know about {forbidden!r}"

    def test_worker_never_references_the_live_mutation_entry_point_directly(self):
        text = (PROJECT_ROOT / "src" / "worker.py").read_text(encoding="utf-8")
        assert "_submit_authorized_order" not in text

    def test_worker_imports_only_customer_autobet_from_the_execution_package(self):
        """Confirms the above isn't just a text-grep artifact -- walks
        the real AST (catching the lazy, function-local import too) and
        confirms src.execution.customer_autobet is the ONLY module
        under src.execution.* reachable from worker.py."""
        import ast
        tree = ast.parse((PROJECT_ROOT / "src" / "worker.py").read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
        execution_imports = {m for m in imported_modules if m.startswith("src.execution")}
        assert execution_imports == {"src.execution.customer_autobet"}, (
            "worker.py must import ONLY src.execution.customer_autobet from the execution "
            f"package, got: {sorted(execution_imports)}"
        )


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
