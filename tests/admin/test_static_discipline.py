"""The frontend's structural rules, architecture-test style: the files THIS package ships are
scanned, so the ban is on the artifact, not on a convention someone remembers.

Why a grep is right here where it was wrong for the ACL predicate: that check needed to prove a
predicate IS used (prose can fake presence); these prove sinks are ABSENT — a match in a comment
would be a false failure, never a false pass, so the check fails safe."""
import pathlib
import re

from stigmergy.admin import routes

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = pathlib.Path(routes.__file__).parent / "static"

BANNED_SINKS = re.compile(
    r"\binnerHTML\b|\bouterHTML\b|\binsertAdjacentHTML\b|\bdocument\.write\b"
    r"|\beval\s*\(|\bnew\s+Function\b")

EXTERNAL_REF = re.compile(r"""(?:src|href)\s*=\s*["']https?://""")

# `el(tag, { ...props... }, ...children)` (ui.js) — the properties object is whatever sits
# between the opening `{` right after the tag string and its balanced closing `}`, whether the
# call is written on one line or several (the Gardener/Index "needs the GitHub token" buttons
# split `disabled:`/`title:` across two lines; a same-line-only regex would miss them).
EL_CALL_PROPS_OPEN = re.compile(r"""\bel\(\s*["'][\w-]+["']\s*,\s*\{""")
TITLE_KEY = re.compile(r"\btitle\s*:")
DISABLED_KEY = re.compile(r"\bdisabled\s*:")


def _element_props_objects(text):
    """Yield `(start_offset, props_substring)` for every `el(tag, {...})` properties object in
    `text`, matched by counting brace depth from the opening `{` — so nested content (an
    `onclick` closure that itself builds an object literal, as the two GitHub-gated dispatch
    buttons do) does not truncate the match early."""
    for call in EL_CALL_PROPS_OPEN.finditer(text):
        start = call.end() - 1
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    yield start, text[start:i + 1]
                    break


def _files(*suffixes):
    found = [p for p in STATIC.rglob("*") if p.suffix in suffixes]
    assert found, f"no {suffixes} files under {STATIC} — the layout moved and this went blind"
    return found


def test_the_static_files_ship_inside_the_package():
    """The wheel packages the package directory whole; if these files move out of it, staging
    serves 404s that no Python test would otherwise notice."""
    assert (STATIC / "index.html").is_file()
    assert (STATIC / "assets" / "app.js").is_file()
    assert (STATIC / "assets" / "styles.css").is_file()


def test_no_html_string_sink_anywhere_in_the_shipped_js():
    offenders = []
    for path in _files(".js", ".html"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if BANNED_SINKS.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "an HTML-string sink appeared in the console frontend — untrusted capture text flows "
        "into these files, so rendering is textContent/DOM-construction ONLY:\n  "
        + "\n  ".join(offenders))


def test_the_shipped_js_builds_dom_with_text_content():
    """The benign twin: the ban above must coexist with the sanctioned mechanism actually being
    used — a frontend that used neither would mean rendering moved somewhere this test is blind."""
    ui = (STATIC / "assets" / "ui.js").read_text(encoding="utf-8")
    assert "createTextNode" in ui or "textContent" in ui


def test_no_element_carries_both_a_title_hint_and_a_disabled_flag():
    """A `disabled` control fires no pointer events in Chrome/Firefox/Safari — no hover, so the
    `title` tooltip never shows — and it cannot receive focus, so keyboard and screen-reader users
    have no path to it either. `title:` alongside `disabled:` on the SAME element is therefore a
    reason NO user, on any input method, can ever reach (issue #35: the queue detail card's three
    disposition buttons). The guard scans every shipped file, not one card — this exact anti-
    pattern is a class of bug, and it turns out to already live in two more places."""
    offenders = []
    for path in _files(".js"):
        text = path.read_text(encoding="utf-8")
        for start, props in _element_props_objects(text):
            title_match = TITLE_KEY.search(props)
            disabled_match = DISABLED_KEY.search(props)
            if not (title_match and disabled_match):
                continue
            title_line = text.count("\n", 0, start + title_match.start()) + 1
            disabled_line = text.count("\n", 0, start + disabled_match.start()) + 1
            source = text.splitlines()[disabled_line - 1].strip()
            offenders.append(f"{path.name}:{disabled_line} (title: line {title_line}): {source}")
    assert not offenders, (
        "an element's properties carry both title: and disabled: — disabled elements get no "
        "pointer events (no hover/tooltip) and no focus (no keyboard/screen-reader path), so the "
        "hint is unreachable by any user; render the reason as visible text instead:\n  "
        + "\n  ".join(offenders))


def test_the_disabled_disposition_hint_is_rendered_as_visible_text():
    """The twin to the ban above: `disabledHint` (views.js) is the sentence explaining why a
    disposition button is disabled. Proving `title:` is gone is not enough — the reason must
    actually land somewhere a user can read it. This asserts `disabledHint` is ALSO used as this
    file's own hints/notes are (`el(tag, {...}, hintVariable)`, e.g. `confirmForm`'s
    `el("span", {class: "hint"}, f.hint)` in ui.js) — passed as a text CHILD — not only ever as an
    attribute value nothing renders."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    assert "const disabledHint" in views, (
        "views.js no longer defines disabledHint by that name — update this check's target")
    text_child_uses = 0
    for match in re.finditer(r"\bdisabledHint\b", views):
        before = views[:match.start()].rstrip()
        if before.endswith("const"):
            continue  # the declaration itself
        if before.endswith(":"):
            continue  # an attribute value (`title: disabledHint`) — not visible text
        text_child_uses += 1
    assert text_child_uses, (
        "disabledHint appears only as its own `const` declaration and as a `title:` attribute "
        "value in views.js — no occurrence is passed as a text child anywhere, so the reason "
        "renders nowhere a user can read it; pass disabledHint as a child argument to a "
        "text-bearing el() the way this file's own inline hints/notes are rendered")


def test_the_console_loads_no_external_resource():
    """Self-contained by CSP AND by construction: no http(s) src/href in any shipped file —
    the only absolute URLs the app ever renders are GitHub run links built from API data."""
    offenders = []
    for path in _files(".js", ".html", ".css"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if EXTERNAL_REF.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "external resource reference in the console:\n  " + "\n  ".join(offenders)


def test_admin_console_docs_do_not_promise_an_unreachable_hover_reason():
    """docs/reference/admin-console.md's Queue bullet promises the three disposition buttons are
    "disabled ... with the reason on hover" — an affordance no browser delivers on a disabled
    control (issue #35; see the two tests above). Pinned the way `tests/test_readme_claims.py`
    pins README claims against the code: a documented affordance must be checkable against what
    ships, or it rots silently the moment the two drift."""
    doc = (ROOT / "docs" / "reference" / "admin-console.md").read_text(encoding="utf-8")
    start = doc.index("- **Queue**")
    end = doc.index("- **Crons**", start)
    queue_bullet = doc[start:end]
    assert "reason on hover" not in queue_bullet, (
        "docs/reference/admin-console.md's Queue bullet still promises 'reason on hover' — a "
        "disabled button fires no pointer events in any browser, so that tooltip never shows; "
        "once the fix renders the reason as visible inline text, restate this sentence to match")
