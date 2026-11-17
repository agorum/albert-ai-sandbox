// Layout check for the admin UI: paste into the browser console on any page of
// the admin UI (signed in). Text that sits side by side in the top bar must share
// one baseline, whatever font the browser uses:
//   leftDiff  - "Admin · Version" against "ALBERT | AI Sandbox"
//   rightDiff - label of "Abmelden" against "Angemeldet als …"
// Expected: both 0 (px). centreOffset: the groups stay centred in the bar
// (all values equal; they differ from 0 only by the bar's bottom border).
// tests/test_admin_layout.py checks the CSS rules this relies on.
(() => {
  const probe = () => {
    const i = document.createElement("i");
    i.style.cssText = "display:inline-block;width:0;height:0;vertical-align:baseline;margin:0;padding:0";
    return i;
  };
  // baseline of inline text: an empty inline-block at the end of the text
  const inline = (el) => { const p = probe(); el.append(p); const y = p.getBoundingClientRect().bottom; p.remove(); return y; };
  // baseline of a button label: the label wrapped in a span with the probe, then restored
  const button = (b) => {
    const saved = [...b.childNodes];
    const s = document.createElement("span");
    s.textContent = b.textContent.trim();
    const p = probe();
    s.append(p);
    b.replaceChildren(s);
    const y = p.getBoundingClientRect().bottom;
    b.replaceChildren(...saved);
    return y;
  };
  const r2 = (v) => Math.round(v * 100) / 100;
  const bar = document.querySelector(".topbar").getBoundingClientRect();
  const mid = (sel) => {
    const r = document.querySelector(sel).getBoundingClientRect();
    return r2((r.top + r.bottom) / 2 - (bar.top + bar.bottom) / 2);
  };
  return {
    leftDiff: r2(inline(document.querySelector(".brand-meta")) - inline(document.querySelector(".brand-app"))),
    rightDiff: r2(button(document.querySelector("#logout-btn")) - inline(document.querySelector("#current-user"))),
    centreOffset: { logo: mid(".brand-logo"), brandText: mid(".brand-text"), userbox: mid(".userbox"),
                    theme: mid("#theme-toggle") },
  };
})();
