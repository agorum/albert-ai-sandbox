"""Static guards for layout rules of the admin UI that were fixed after a review.

The real check runs in a browser (tests/ui_baselines.js); these tests only make
sure the rules it relies on are not lost in later edits.
"""
import re
from pathlib import Path

ADMIN = Path(__file__).resolve().parent.parent / "scripts" / "albert_sandbox" / "static" / "admin"


def _rule(css: str, selector: str) -> str:
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"rule {selector} missing"
    return match.group(1)


def test_top_bar_texts_share_a_baseline():
    css = (ADMIN / "app.css").read_text(encoding="utf-8")
    html = (ADMIN / "index.html").read_text(encoding="utf-8")
    # product name and "Admin · Version" in one baseline group
    assert "align-items: baseline" in _rule(css, ".brand-text")
    assert re.search(r'<span class="brand-text">\s*<span class="brand-app">.*?</span>\s*'
                     r'<span class="brand-meta">', html, re.S)
    meta = _rule(css, ".brand-meta")
    assert "align-self" not in meta and "margin" not in meta  # no hand-made offset
    # "Angemeldet als …" and the label of "Abmelden"
    assert "align-items: baseline" in _rule(css, ".userbox")
