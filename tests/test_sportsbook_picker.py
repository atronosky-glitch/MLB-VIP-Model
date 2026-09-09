"""Functional tests for src/sportsbook_picker.py using Streamlit's own
AppTest harness -- this actually runs the picker inside a real Streamlit
script and simulates checkbox/button clicks, rather than just asserting
on source text."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def _make_app(tmp_path, books, key_prefix="t"):
    script = tmp_path / "picker_app.py"
    script.write_text(
        "import streamlit as st\n"
        "from src.sportsbook_picker import render_sportsbook_picker\n"
        f"selected = render_sportsbook_picker({books!r}, key_prefix={key_prefix!r})\n"
        "st.session_state['_result'] = sorted(selected)\n",
        encoding="utf-8",
    )
    at = AppTest.from_file(str(script))
    at.run()
    return at


class TestSportsbookPicker:
    def test_defaults_to_every_book_selected(self, tmp_path):
        at = _make_app(tmp_path, ["draftkings", "fanduel", "betmgm"])
        assert at.session_state["_result"] == ["betmgm", "draftkings", "fanduel"]

    def test_unchecking_a_book_removes_it_from_selection(self, tmp_path):
        at = _make_app(tmp_path, ["draftkings", "fanduel", "betmgm"])
        for cb in at.checkbox:
            if cb.label == "FanDuel":
                cb.uncheck()
        at.run()
        assert at.session_state["_result"] == ["betmgm", "draftkings"]

    def test_rechecking_a_book_restores_it(self, tmp_path):
        at = _make_app(tmp_path, ["draftkings", "fanduel"])
        for cb in at.checkbox:
            if cb.label == "FanDuel":
                cb.uncheck()
        at.run()
        for cb in at.checkbox:
            if cb.label == "FanDuel":
                cb.check()
        at.run()
        assert at.session_state["_result"] == ["draftkings", "fanduel"]

    def test_clear_all_deselects_every_book(self, tmp_path):
        at = _make_app(tmp_path, ["draftkings", "fanduel", "betmgm"])
        for b in at.button:
            if b.label == "Clear all":
                b.click()
        at.run()
        assert at.session_state["_result"] == []

    def test_select_all_reselects_every_book(self, tmp_path):
        at = _make_app(tmp_path, ["draftkings", "fanduel", "betmgm"])
        for b in at.button:
            if b.label == "Clear all":
                b.click()
        at.run()
        for b in at.button:
            if b.label == "Select all":
                b.click()
        at.run()
        assert at.session_state["_result"] == ["betmgm", "draftkings", "fanduel"]

    def test_unknown_books_display_a_title_cased_name(self, tmp_path):
        at = _make_app(tmp_path, ["some_regional_book"])
        labels = {cb.label for cb in at.checkbox}
        assert "Some Regional Book" in labels

    def test_empty_book_list_renders_no_popover(self, tmp_path):
        at = _make_app(tmp_path, [])
        assert at.session_state["_result"] == []
        assert len(at.checkbox) == 0

    def test_no_exceptions_raised(self, tmp_path):
        at = _make_app(tmp_path, ["draftkings", "fanduel", "betmgm", "caesars"])
        assert not at.exception


def test_customer_view_and_control_panel_use_the_shared_picker():
    """Regression check: the multiselect that used to render bright-lime
    tag chips should be gone from both surfaces, replaced by the shared
    popover picker (2026-09-09, operator feedback: wanted a dropdown of
    tick-boxes with a per-book badge, not the default theme color)."""
    for filename in ("customer_view.py", "control_panel.py"):
        source = (ROOT / "src" / filename).read_text(encoding="utf-8")
        assert "from src.sportsbook_picker import render_sportsbook_picker" in source
        assert "render_sportsbook_picker(" in source
        assert "Which sportsbooks do you have accounts at?" not in source
