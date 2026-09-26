"""Static regression guards for customer-site visibility/contrast.

Every rule here traces to a real defect found by rendering the page and reading
computed styles (2026-09-26): near-white inactive tabs and radio labels, a dark-on-dark
expander header, near-white stepper/tooltip/password icons, 0.6-opacity captions and a
white-on-green button below 4.5:1.  The module is read as TEXT and never imported: importing
src.customer_view outside Streamlit leaks st.form contexts into AppTest suites.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "customer_view.py").read_text(encoding="utf-8")
CSS = re.sub(r"/\*.*?\*/", "", SRC[SRC.index("<style>"): SRC.index("</style>")], flags=re.S)


def _token(name: str) -> str:
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert match, f"--{name} token missing"
    return match.group(1)


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(fg: str, bg: str) -> float:
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rule(selector: str) -> str:
    """Concatenated bodies of every CSS rule whose full selector list equals `selector`
    (whitespace-normalised), in source order -- i.e. everything the cascade applies."""
    norm = lambda text: re.sub(r"\s+", " ", text).strip()
    bodies = [m.group(2) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS) if norm(m.group(1)) == norm(selector)]
    assert bodies, f"no CSS rule for {selector!r}"
    return " ".join(bodies)


class TestTokenContrast:
    def test_body_text_tokens_meet_aa_on_white_and_panel(self):
        for name in ("ink", "muted", "accent-soft"):
            for bg in ("#ffffff", _token("panel")):
                assert _contrast(_token(name), bg) >= 4.5, f"--{name} on {bg}"

    def test_win_green_supports_white_button_text_and_text_on_panel(self):
        assert _contrast("#ffffff", _token("win")) >= 4.5      # .bet-now-btn label
        assert _contrast(_token("win"), _token("panel")) >= 4.5  # .result-win
        assert _contrast(_token("loss"), _token("panel")) >= 4.5  # .result-loss

    def test_big_number_colors_are_aa(self):
        for color in re.findall(r'color = "(#[0-9a-f]{6})" if total >= 0', SRC):
            assert _contrast(color, _token("panel")) >= 4.5


class TestPrimaryActions:
    def test_submit_and_primary_buttons_are_solid_dark_with_white_text_and_edge(self):
        body = _rule('[data-testid="stBaseButton-primaryFormSubmit"], [data-testid="stBaseButton-primary"]')
        assert "min-height" in body and "box-shadow" in body and "border:2px solid" in body
        base = _rule('[data-testid="stBaseButton-primaryFormSubmit"]')
        assert "background-color:var(--accent)" in base and "color:#fff" in base
        assert _contrast("#ffffff", _token("accent")) >= 7

    def test_secondary_buttons_have_a_visible_border(self):
        body = _rule('[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"]')
        assert "border:2px solid var(--accent-soft)" in body

    def test_disabled_buttons_are_readable_and_visibly_inert(self):
        body = _rule('[data-testid^="stBaseButton"]:disabled, [data-testid="stPopoverButton"]:disabled')
        assert "dashed" in body and "not-allowed" in body and "opacity:1" in body
        assert _contrast("#374151", "#e5e7eb") >= 4.5

    def test_hover_active_and_focus_states_exist(self):
        assert '[data-testid="stBaseButton-primaryFormSubmit"]:hover' in CSS
        assert '[data-testid="stBaseButton-primaryFormSubmit"]:active' in CSS
        assert "focus-visible" in CSS and "outline:3px solid" in CSS


class TestNoNearWhiteOrFaintText:
    def test_inactive_tabs_are_dark(self):
        assert "color:var(--accent-soft)" in _rule('[data-testid="stTab"] p')

    def test_expander_header_is_light_with_dark_text(self):
        body = _rule('[data-testid="stExpander"] summary, [data-testid="stExpander"] summary:hover, [data-testid="stExpander"] details[open] > summary, [data-testid="stExpander"] summary:focus')
        assert "background-color:#ffffff" in body and "color:var(--ink)" in body

    def test_captions_are_full_opacity(self):
        assert "opacity:1" in _rule('[data-testid="stCaptionContainer"]')
        assert not re.search(r"opacity:\s*\.6", CSS)

    def test_placeholder_and_password_icon_are_visible(self):
        assert "::placeholder" in CSS and "color:#6b7280" in _rule('[data-testid="stTextInputRootElement"] input::placeholder, [data-testid="stNumberInput"] input::placeholder, [data-testid="stTextArea"] textarea::placeholder')
        assert _contrast("#6b7280", "#ffffff") >= 4.5
        assert "color:var(--accent-soft)" in _rule('[data-testid="stTextInputRootElement"] button, [data-testid="stTextInputRootElement"] button *')

    def test_stepper_tooltip_and_select_icons_are_dark(self):
        assert "fill:var(--ink)" in _rule('[data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"], [data-testid="stNumberInputStepUp"] svg, [data-testid="stNumberInputStepDown"] svg')
        assert "fill:var(--accent-soft)" in _rule('[data-testid="stTooltipIcon"], [data-testid="stTooltipIcon"] *, [data-testid="stSelectbox"] svg, [data-testid="stMultiSelect"] svg')

    def test_radio_labels_are_dark_and_not_painted_by_the_broad_selected_rule(self):
        assert "color:var(--ink)" in _rule('[data-testid="stRadioOption"] p, [data-testid="stRadioOption"] [data-testid="stMarkdownContainer"]')
        # the old rule painted the label text container (not just the dot) with the accent color
        assert '[data-selected="true"] > div > div > div {' not in CSS

    def test_control_edges_meet_3_to_1(self):
        assert _contrast("#6b7280", "#ffffff") >= 3
        assert "border:1px solid #6b7280" in _rule('[data-testid="stSelectbox"] .react-aria-ComboBox > div[role="group"], [data-testid="stMultiSelect"] [data-baseweb="select"] > div, [data-testid="stPopoverButton"], [data-testid="stNumberInputContainer"]')

    def test_toggle_has_visible_track_and_knob(self):
        assert 'input[role="switch"]' in CSS
        assert "border:2px solid var(--accent-soft)" in _rule('[data-testid="stCheckbox"] label:has(input[role="switch"]) > div:not([data-testid])')


class TestNoNcaafLabel:
    def test_hero_and_footer_do_not_advertise_ncaaf(self):
        assert 'eyebrow">MLB · NFL · WNBA<' in SRC
        assert "NCAAF &nbsp;&mdash;" not in SRC
