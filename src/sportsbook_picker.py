"""Shared "which sportsbooks do you have?" dropdown picker.

Used on both the customer site and the admin dashboard so arbitrage and
middling opportunities can be filtered down to only the ones a viewer can
actually place (both legs at a book they've ticked on). A tick-box list
inside a popover reads clearer than a multiselect's tag chips once each
row also carries a colored book badge, and it lets us recolor the
checked state away from the app theme's default bright-lime accent.
"""

from __future__ import annotations

import streamlit as st

from src.sportsbook_branding import sportsbook_badge_html, sportsbook_display

_PICKER_CSS = """
<style>
div[data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p {
    font-weight: 600;
}
/* Recolor the checkbox check away from the theme's bright-lime primary
   accent to gold, matching the rest of the product's dark/gold styling.
   Streamlit renders the visible box as the checkbox label's 2nd child
   div (a real <input type=checkbox> sits hidden alongside it), toggled
   via the label's own data-selected attribute. */
div[data-testid="stCheckbox"] label[data-selected="true"] > div:nth-child(2) {
    background-color: #e8b923 !important;
    border-color: #e8b923 !important;
}
</style>
"""


def render_sportsbook_picker(
    all_books: list[str],
    key_prefix: str,
    label: str = "Filter by sportsbook",
) -> set[str]:
    """Popover checklist, one row per book (badge + name + checkbox).

    Defaults to every book ticked. Backed by st.session_state per
    checkbox, so unticking a book persists across reruns until the
    viewer changes it again or the "Select all" / "Clear all" shortcuts
    are used.
    """
    if not all_books:
        return set()
    st.markdown(_PICKER_CSS, unsafe_allow_html=True)

    with st.popover(f"🎚️ {label}"):
        st.caption(f"Untick a book you don't have an account at — {len(all_books)} tracked.")
        pick_col, clear_col = st.columns(2)
        if pick_col.button("Select all", key=f"{key_prefix}_pick_all", use_container_width=True):
            for book in all_books:
                st.session_state[f"{key_prefix}_book_{book}"] = True
        if clear_col.button("Clear all", key=f"{key_prefix}_clear_all", use_container_width=True):
            for book in all_books:
                st.session_state[f"{key_prefix}_book_{book}"] = False
        st.divider()
        selected: set[str] = set()
        for book in all_books:
            widget_key = f"{key_prefix}_book_{book}"
            if widget_key not in st.session_state:
                st.session_state[widget_key] = True
            badge_col, check_col = st.columns([1, 7])
            with badge_col:
                st.markdown(sportsbook_badge_html(book), unsafe_allow_html=True)
            with check_col:
                if st.checkbox(sportsbook_display(book), key=widget_key):
                    selected.add(book)

    return selected
