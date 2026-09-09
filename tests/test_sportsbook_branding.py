"""Tests for src/sportsbook_branding.py -- the display-name/badge helpers
backing the sportsbook picker used on both the customer site and the
admin dashboard."""

from src.sportsbook_branding import (
    sportsbook_display, sportsbook_badge_html, _initials, _accent_color,
)


class TestSportsbookDisplay:
    def test_known_slug_uses_curated_display_name(self):
        assert sportsbook_display("draftkings") == "DraftKings"
        assert sportsbook_display("fanduel") == "FanDuel"
        assert sportsbook_display("betmgm") == "BetMGM"

    def test_is_case_and_whitespace_insensitive(self):
        assert sportsbook_display(" DraftKings ") == "DraftKings"
        assert sportsbook_display("BETMGM") == "BetMGM"

    def test_williamhill_slug_displays_as_caesars(self):
        """The odds API reports Caesars under the legacy 'williamhill' key."""
        assert sportsbook_display("williamhill") == "Caesars"

    def test_unknown_slug_falls_back_to_title_cased_words(self):
        assert sportsbook_display("some_new_book") == "Some New Book"

    def test_empty_or_none_is_unknown(self):
        assert sportsbook_display(None) == "Unknown"
        assert sportsbook_display("") == "Unknown"


class TestInitials:
    def test_known_slug_uses_curated_initials(self):
        assert _initials("draftkings", "DraftKings") == "DK"
        assert _initials("betmgm", "BetMGM") == "MGM"

    def test_unknown_multi_word_name_uses_first_letters(self):
        assert _initials("some_new_book", "Some New Book") == "SN"

    def test_unknown_single_word_name_uses_first_two_letters(self):
        assert _initials("acme", "Acme") == "AC"


class TestAccentColor:
    def test_known_books_get_their_curated_color(self):
        assert _accent_color("draftkings") == "#e8b923"
        assert _accent_color("fanduel") == "#48d8ff"

    def test_unknown_book_gets_a_stable_fallback_color(self):
        color_a = _accent_color("some_new_book")
        color_b = _accent_color("some_new_book")
        assert color_a == color_b
        assert color_a.startswith("#")

    def test_unknown_books_are_not_all_the_same_color(self):
        colors = {_accent_color(name) for name in ["aaa", "zzz_book", "mid_tier_book", "another_one"]}
        assert len(colors) > 1

    def test_deterministic_across_processes_not_python_hash_randomization(self):
        """Regression guard: must not depend on Python's randomized hash()
        (PYTHONHASHSEED), or the same book would get a different badge
        color every time the customer site or admin dashboard restarts."""
        import os
        import subprocess
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = "from src.sportsbook_branding import _accent_color; print(_accent_color('an_unlisted_book'))"
        results = set()
        for seed in ("0", "1", "42"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            proc = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, cwd=root, env=env,
            )
            assert proc.returncode == 0, proc.stderr
            results.add(proc.stdout.strip())
        assert len(results) == 1


class TestBadgeHtml:
    def test_badge_includes_initials_and_title(self):
        html = sportsbook_badge_html("draftkings")
        assert "DK" in html
        assert 'title="DraftKings"' in html

    def test_badge_includes_accent_color(self):
        html = sportsbook_badge_html("fanduel")
        assert "#48d8ff" in html

    def test_badge_is_a_single_inline_span(self):
        html = sportsbook_badge_html("betmgm")
        assert html.strip().startswith("<span")
        assert html.strip().endswith("</span>")
