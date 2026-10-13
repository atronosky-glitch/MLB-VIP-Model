"""Creator / referral codes: ATTRIBUTION ONLY.

A code (or a ``?ref=CODE`` link) is remembered at signup so you can see which creator brought which
accounts. It does not apply any discount and moves no money: there is no billing system yet. Payouts,
discounts and revenue share need a payments provider plus a written agreement with each creator
(creators must disclose the relationship to their audience, e.g. #ad / #affiliate).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")


def normalize_code(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    code = raw.strip().upper()
    return code if _CODE_RE.match(code) else None


def resolve_code(conn, raw) -> str | None:
    """The normalized code if it exists and is active, else None."""
    code = normalize_code(raw)
    if not code:
        return None
    row = conn.execute("SELECT code FROM creator_codes WHERE code = ? AND active = 1", (code,)).fetchone()
    return code if row else None


def create_code(conn, code: str, creator_name: str, note: str | None = None) -> str:
    norm = normalize_code(code)
    if not norm:
        raise ValueError("Codes are 2-32 characters: letters, numbers, - or _.")
    if not (creator_name or "").strip():
        raise ValueError("Creator name is required.")
    if conn.execute("SELECT code FROM creator_codes WHERE code = ?", (norm,)).fetchone():
        raise ValueError(f"Code {norm} already exists.")
    conn.execute(
        "INSERT INTO creator_codes (code, creator_name, active, note, created_at) VALUES (?, ?, 1, ?, ?)",
        (norm, creator_name.strip(), (note or "").strip() or None, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return norm


def set_active(conn, code: str, active: bool) -> None:
    conn.execute("UPDATE creator_codes SET active = ? WHERE code = ?", (1 if active else 0, code))
    conn.commit()


def creator_stats(conn) -> list[dict]:
    """One row per code with signup counts (all time / last 7 days / email-verified / opted in to marketing)."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    codes = [dict(r) for r in conn.execute("SELECT * FROM creator_codes ORDER BY created_at DESC").fetchall()]
    out = []
    for c in codes:
        rows = [dict(r) for r in conn.execute(
            "SELECT email_verified, marketing_consent, created_at FROM customer_accounts WHERE referral_code = ?",
            (c["code"],)).fetchall()]
        out.append({
            **c, "signups": len(rows),
            "signups_7d": sum(1 for r in rows if str(r["created_at"]).replace(" ", "T") >= week_ago),
            "verified": sum(1 for r in rows if r["email_verified"]),
            "marketing_opt_in": sum(1 for r in rows if r["marketing_consent"]),
        })
    return out


def render_creators_admin(conn, st) -> None:
    """Operator-only page (admin dashboard)."""
    import os

    import pandas as pd

    st.caption("Attribution only: codes and ?ref= links record which creator brought each signup. No discounts or "
               "payouts are applied automatically (no billing system yet). Creators must disclose the relationship.")
    base = (os.environ.get("SITE_BASE_URL") or "https://YOUR-SITE").rstrip("/")
    stats = creator_stats(conn)
    if stats:
        st.dataframe(pd.DataFrame([{
            "Code": s["code"], "Creator": s["creator_name"], "Active": bool(s["active"]), "Signups": s["signups"],
            "Last 7d": s["signups_7d"], "Email-verified": s["verified"], "Marketing opt-in": s["marketing_opt_in"],
            "Link": f"{base}/?ref={s['code']}",
        } for s in stats]), use_container_width=True, hide_index=True)
    else:
        st.info("No creator codes yet.")
    c1, c2, c3 = st.columns(3)
    code = c1.text_input("Code (e.g. ALEX10)", key="creator_new_code")
    name = c2.text_input("Creator name", key="creator_new_name")
    note = c3.text_input("Deal note (private)", key="creator_new_note")
    if st.button("Create code", key="creator_new_btn"):
        try:
            st.success(f"Created {create_code(conn, code, name, note)}")
        except ValueError as exc:
            st.error(str(exc))
    if stats:
        pick = st.selectbox("Activate / deactivate", [s["code"] for s in stats], key="creator_toggle")
        cur = next(s for s in stats if s["code"] == pick)
        if st.button("Deactivate" if cur["active"] else "Activate", key="creator_toggle_btn"):
            set_active(conn, pick, not cur["active"])
            st.rerun()
