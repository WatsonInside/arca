"""Deterministic design checks for the WKWebView UI.

Inspired by impeccable's detector idea: the rules that *can* be checked
mechanically should be checked mechanically, on every run, without an LLM.
Exit code 0 = clean, 2 = findings (so it can gate CI), 1 = could not scan.

What it covers (see docs/DESIGN.md for the reasoning):

* motion      -- transition:all, ease-in on UI, scale(0) entrances,
                 interaction durations over 300ms
* colour      -- contrast of text tokens against the surface they sit on,
                 pure #000/#fff text, forbidden gradient/glow shortcuts
* a11y        -- icon-only buttons need aria-label, focus-visible must exist,
                 reduced-motion block must exist
* typography  -- numeric classes must use tabular-nums
* layout      -- long-text containers need min-width:0 + wrapping

Run:  build/venv/bin/python scripts/design_audit.py [--json]
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "src", "arca", "web")
CSS = os.path.join(WEB, "styles.css")
HTML = os.path.join(WEB, "index.html")
JS = os.path.join(WEB, "app.js")

findings: list[dict] = []


def add(rule: str, file: str, detail: str, line: int | None = None) -> None:
    findings.append({"rule": rule, "file": os.path.relpath(file, ROOT),
                     "line": line, "detail": detail})


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def line_of(text: str, needle: str) -> int | None:
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else None


# --------------------------------------------------------------------- colour
def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def token_map(css: str, selector: str) -> dict[str, str]:
    """Very small token reader: pulls --name: #rrggbb inside one block."""
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    if not m:
        return {}
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{3,8})", m.group(1)))


def check_contrast(css: str) -> None:
    themes = (("light", ":root {"), ("dark", '[data-theme="dark"] {'))
    for name, sel in themes:
        tokens = token_map(css, sel.rstrip(" {") if sel.endswith("{") else sel)
        surface = tokens.get("--surface")
        bg = tokens.get("--bg")
        if not surface:
            continue
        for key, minimum in (("--ink", 7.0), ("--ink-2", 5.0), ("--ink-3", 4.5)):
            val = tokens.get(key)
            if not val or not val.startswith("#"):
                continue
            ratio = contrast(val, surface)
            if ratio < minimum:
                add("contrast", CSS,
                    f"{name} {key} {val} on {surface} = {ratio:.2f}:1 "
                    f"(needs {minimum}:1)", line_of(css, f"{key}: {val}"))
            elif bg and key == "--ink-3":
                ratio_bg = contrast(val, bg)
                if ratio_bg < 4.5:
                    add("contrast", CSS,
                        f"{name} {key} {val} on page bg {bg} = {ratio_bg:.2f}:1 "
                        "(needs 4.5:1)", line_of(css, f"{key}: {val}"))


# --------------------------------------------------------------------- motion
DURATION_RE = re.compile(r"(\d*\.?\d+)(m?s)\b")


def _ms(num: str, unit: str) -> float:
    return float(num) * (1 if unit == "ms" else 1000)


def check_motion(css: str) -> None:
    if "transition: all" in css:
        add("motion-transition-all", CSS, "transition: all",
            line_of(css, "transition: all"))

    # ease-in alone (not ease-in-out) on interactive elements
    for m in re.finditer(r"(transition|animation):[^;{}]*\bease-in\b(?!-out)", css):
        add("motion-ease-in", CSS,
            "ease-in delays the first frame the eye watches; use --ease-out",
            css.count("\n", 0, m.start()) + 1)

    if re.search(r"scale\(\s*0\s*\)", css):
        add("motion-scale-zero", CSS,
            "entrances must start at scale(0.955)+opacity:0, never scale(0)",
            line_of(css, "scale(0)"))

    # Interaction feedback budget: anything outside @keyframes marquee must be
    # <= 300ms.  The indeterminate bar is deliberate continuous motion.
    for m in re.finditer(r"transition:\s*([^;{}]+);", css):
        body = m.group(1)
        for num, unit in DURATION_RE.findall(body):
            if _ms(num, unit) > 320:
                add("motion-too-slow", CSS,
                    f"{body.strip()[:70]} ({num}{unit})",
                    css.count("\n", 0, m.start()) + 1)
    for m in re.finditer(r"animation:\s*([^;{}]+);", css):
        body = m.group(1)
        if "infinite" in body:
            continue                      # marquee / spinner: continuous by design
        for num, unit in DURATION_RE.findall(body):
            if _ms(num, unit) > 320:
                add("motion-too-slow", CSS,
                    f"{body.strip()[:70]} ({num}{unit})",
                    css.count("\n", 0, m.start()) + 1)


# ----------------------------------------------------------------------- a11y
def check_a11y(css: str, html: str) -> None:
    if ":focus-visible" not in css:
        add("a11y-focus-visible", CSS, "no :focus-visible rule at all")
    if "prefers-reduced-motion" not in css:
        add("a11y-reduced-motion", CSS, "no prefers-reduced-motion block")

    for m in re.finditer(r"<button[^>]*class=\"[^\"]*icon-btn[^\"]*\"[^>]*>", html):
        tag = m.group(0)
        if "aria-label" not in tag:
            add("a11y-icon-label", HTML,
                f"icon-only button without aria-label: {tag[:80]}",
                html.count("\n", 0, m.start()) + 1)

    # Interactive elements built from divs need a role.
    for m in re.finditer(r"<div[^>]*class=\"([^\"]*)\"[^>]*>", html):
        tag, classes = m.group(0), m.group(1).split()
        # Only a div that *is* a control (class token btn/toggle), not a
        # container that merely mentions buttons in its class name.
        if not ({"btn", "toggle"} & set(classes)):
            continue
        if "role=" not in tag:
            add("a11y-role", HTML, f"div acting as control without role: {tag[:70]}",
                html.count("\n", 0, m.start()) + 1)


# ---------------------------------------------------------------------- icons
def check_icons(css: str, html: str) -> None:
    """Icon consistency: one weight source, one size scale, sane optical boxes."""
    # 1) stroke-width must live in CSS, never on the symbol: a presentation
    #    attribute beats inherited CSS, so per-size compensation silently dies.
    for m in re.finditer(r"<symbol[^>]*stroke-width", html):
        add("icon-weight-source", HTML,
            "symbol carries its own stroke-width; it overrides the CSS scale",
            html.count("\n", 0, m.start()) + 1)

    # 2) display sizes should come from a small scale (13/16/22 + hero 30/44)
    allowed = {"11px", "13px", "14px", "16px", "22px", "30px", "44px"}
    for m in re.finditer(r"\.([\w-]+(?:\s+svg)?)\s*\{[^}]*?width:\s*(\d+)px", css):
        sel, px = m.group(1), m.group(2)
        if "svg" not in sel or px + "px" in allowed:
            continue
        add("icon-size-scale", CSS,
            f"{sel} renders icons at {px}px (allowed: {'/'.join(sorted(allowed))})",
            css.count("\n", 0, m.start()) + 1)

    # 3) every icon must opt into the shared weight (i.e. CSS base exists)
    if not re.search(r"^svg\s*\{\s*stroke-width", css, re.M):
        add("icon-base-weight", CSS, "no base svg { stroke-width } rule")


# ----------------------------------------------------------------- typography
def check_typography(css: str) -> None:
    numeric = re.findall(r"\n([.\w-]+)\s*\{([^}]*)\}", css)
    wanted = (".count", ".file-size", ".level-name", ".card-head .meta",
              ".engine-card small", ".progress-pct", ".job-meta")
    for sel, body in numeric:
        s = sel.strip()
        if s in wanted and "font-variant-numeric" not in body:
            add("type-tabular-nums", CSS, f"{s} shows numbers without tabular-nums",
                line_of(css, s + " {"))
    if "text-wrap: balance" not in css:
        add("type-balance", CSS, "headings do not use text-wrap: balance")


# --------------------------------------------------------------------- layout
def check_layout(css: str) -> None:
    if "overflow-wrap: anywhere" not in css:
        add("layout-wrap", CSS, "no overflow-wrap: anywhere for long names")
    if "z-index: 90" in css or "z-index: 70" in css or "z-index: 80" in css:
        add("layout-z-index", CSS, "raw z-index values outside the token scale")


# ------------------------------------------------------------------ anti-slop
ANTI = [
    # A violet gradient as a *surface* is the classic AI slop; the brand mark
    # is the app's own logo (same gradient as the ICNS), so it is exempt.
    (r"(?<!brand)\n(?!\.brand-mark)[^.\n][^\n]*\{[^}]*linear-gradient\([^;]*#8b5cf6",
     "purple-gradient", "violet gradient used as a surface (brand mark is exempt)"),
    (r"box-shadow:[^;]*0 0 \d+px rgba?\([^)]*0\.\d\),[^;]*0 0 \d+px", "dark-glow",
     "glow used as an affordance"),
]


def check_anti(css: str) -> None:
    for pat, rule, detail in ANTI:
        if re.search(pat, css):
            add(rule, CSS, detail)


def main() -> int:
    if not (os.path.exists(CSS) and os.path.exists(HTML)):
        print("cannot find web assets", file=sys.stderr)
        return 1
    css, html = read(CSS), read(HTML)
    check_contrast(css)
    check_motion(css)
    check_a11y(css, html)
    check_icons(css, html)
    check_typography(css)
    check_layout(css)
    check_anti(css)

    if "--json" in sys.argv:
        print(json.dumps({"findings": findings}, ensure_ascii=False, indent=2))
    else:
        if not findings:
            print("design audit: clean")
        for f in findings:
            where = f["file"] + (f":{f['line']}" if f["line"] else "")
            print(f"[{f['rule']}] {where}\n    {f['detail']}")
        print(f"\n{len(findings)} finding(s)")
    return 0 if not findings else 2


if __name__ == "__main__":
    sys.exit(main())
