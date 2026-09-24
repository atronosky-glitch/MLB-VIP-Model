"""Renders the My Performance section with Streamlit's AppTest against a
temporary SQLite file (never the production DB) and asserts the customer
sees an honest label: gross vs net, estimated fees, unfilled/partial/
refund-pending notes, and unconfirmed-fill warnings."""

from __future__ import annotations

import uuid

import pytest
from streamlit.testing.v1 import AppTest

from database.db_manager import init_db, save_autobet_execution
import database.db_manager as dbm


def _script(db_file: str, mode: str = "LIVE") -> None:
    """Runs inside AppTest's script thread. src/customer_view.py executes
    the whole page at import time, so the two render functions are
    extracted from its source (AST) and exec'd in a controlled namespace."""
    import ast
    from pathlib import Path

    import altair as alt
    import pandas as pd
    import streamlit as st

    import database.db_manager as dbm_inner
    from src.customer_accounts import Account
    from src.customer_performance import get_customer_performance

    source = Path("src/customer_view.py").read_text(encoding="utf-8")
    wanted = {"_performance_cumulative_chart", "_render_my_performance"}
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {
        "st": st, "pd": pd, "alt": alt, "Account": Account, "annotations": None,
        "get_connection": lambda *a, **k: dbm_inner.get_connection(db_file),
        "get_customer_performance": get_customer_performance,
        "get_autobet_executions": dbm_inner.get_autobet_executions,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "customer_view_extract", "exec"), namespace)
    account = Account(account_id="acct-1", email="a@b.c", phone=None, email_verified=True,
                      phone_verified=False, marketing_consent=False)
    if mode == "PAPER":
        st.session_state["perf_mode"] = "PAPER"
    namespace["_render_my_performance"](account)


def _exec(**overrides):
    base = {
        "execution_id": str(uuid.uuid4()), "account_id": "acct-1", "recommendation_id": "rec-1",
        "market_type": "moneyline", "matchup": "A @ B", "side": "YES", "stake_usd": 25.0,
        "requested_quantity": 20.0, "filled_quantity": 20.0, "avg_fill_price": 0.50,
        "fees_usd": None, "fees_source": None, "fill_source": "ORDER_LIMIT_PRICE",
        "status": "EXECUTED", "mode": "LIVE", "approval_mode": "AUTO", "platform": "kalshi",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def db_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = str(tmp_path / "ui.db")
    init_db(path)
    return path


def _settle(path, rec, status):
    conn = dbm.get_connection(path)
    conn.execute(
        "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
        "VALUES (?, ?, ?, '2026-09-20T00:00:00+00:00')", (str(uuid.uuid4()), rec, status))
    conn.commit()
    conn.close()


def _seed(path, *rows):
    conn = dbm.get_connection(path)
    for row in rows:
        save_autobet_execution(conn, row)
    conn.close()


def _text(at: AppTest) -> str:
    parts = [c.value for c in at.caption] + [m.label + " " + str(m.value) for m in at.metric]
    return "\n".join(str(p) for p in parts)


def _run(db_file, mode="LIVE"):
    at = AppTest.from_function(_script, args=(db_file, mode), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_missing_fees_render_a_gross_label_never_net(db_file):
    _seed(db_file, _exec())
    _settle(db_file, "rec-1", "WIN")
    text = _text(_run(db_file))
    assert "Realized Gross P&L $+10.00" in text
    assert "Gross" in text and "fees are not available" in text
    assert "Realized Net P&L" not in text


def test_platform_fees_render_a_net_label_with_the_fee_deducted(db_file):
    _seed(db_file, _exec(fees_usd=0.35, fees_source="PLATFORM", fill_source="PLATFORM_FILLS"))
    _settle(db_file, "rec-1", "WIN")
    text = _text(_run(db_file))
    assert "Realized Net P&L $+9.65" in text
    assert "Net of platform-reported fees" in text


def test_estimated_fees_are_disclosed_and_unconfirmed_fills_are_flagged(db_file):
    _seed(db_file, _exec(fees_usd=0.35, fees_source="ESTIMATED"))
    _settle(db_file, "rec-1", "LOSS")
    text = _text(_run(db_file))
    assert "fees are estimated" in text.lower() or "estimated from the platform" in text.lower()
    assert "still use the order's limit price" in text


def test_unfilled_partial_and_refund_pending_are_called_out(db_file):
    _seed(
        db_file,
        _exec(recommendation_id="r-unfilled", filled_quantity=0.0, avg_fill_price=None, fill_source=None),
        _exec(recommendation_id="r-partial", status="PARTIALLY_FILLED", filled_quantity=8.0),
        _exec(recommendation_id="r-push"),
    )
    _settle(db_file, "r-partial", "WIN")
    _settle(db_file, "r-push", "PUSH")
    text = _text(_run(db_file))
    assert "didn't fill" in text
    assert "partial fill" in text
    assert "push/void" in text and "excluded until the platform refund is confirmed" in text


def test_open_positions_are_exposure_not_pnl(db_file):
    _seed(db_file, _exec())          # never graded
    at = _run(db_file)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Open Positions"] == "1"
    assert metrics["Open Exposure"] == "$10.00"     # filled cost, not the $25 intended stake
