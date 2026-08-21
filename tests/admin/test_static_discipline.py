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
# The frontend is one module per view under `assets/views/`; the three this file reads by name are
# the captures view (the disposition buttons), the entities view (the mint form) and the repairs
# view (the per-kind change renderers). A check that went looking for a monolith would go blind.
CAPTURES_VIEW = STATIC / "assets" / "views" / "captures.js"
ENTITIES_VIEW = STATIC / "assets" / "views" / "entities.js"
REPAIRS_VIEW = STATIC / "assets" / "views" / "repairs.js"

BANNED_SINKS = re.compile(
    r"\binnerHTML\b|\bouterHTML\b|\binsertAdjacentHTML\b|\bdocument\.write\b"
    r"|\beval\s*\(|\bnew\s+Function\b")

# Both spellings of an attribute — HTML (`href="https://…"`) and the object-literal form this
# frontend actually writes (`href: "https://…"`) — plus a stylesheet `url(https://…)`.
EXTERNAL_REF = re.compile(r"""(?:src|href)\s*[:=]\s*["']https?://|url\(\s*["']?https?://""")

# `el(tag, { ...props... }, ...children)` (ui.js) — the properties object is whatever sits
# between the opening `{` right after the tag string and its balanced closing `}`, whether the
# call is written on one line or several (the Gardener/Index "needs the GitHub token" buttons
# split `disabled:`/`title:` across two lines; a same-line-only regex would miss them).
EL_CALL_PROPS_OPEN = re.compile(r"""\bel\(\s*["'][\w-]+["']\s*,\s*\{""")
TITLE_KEY = re.compile(r"\btitle\s*:")
DISABLED_KEY = re.compile(r"\bdisabled\s*:")

# `row.subject` — the JOINED display string (`entities.situations.subject_of`, `", ".join(names)`).
# `row.subjects` (the per-name list `subject_of`'s docstring sends independent actors to) does NOT
# match: `subject` followed by `s` is not a word boundary.
JOINED_SUBJECT = re.compile(r"\.subject\b")
OBJECT_KEY = re.compile(r"([A-Za-z_$][\w$]*)\s*:")
# A prefill decided by "is there exactly one name": a comparison between some `.length` and 1 or 2,
# written in whichever direction the fix picks (`=== 1`, `> 1`, `< 2`, `!== 1`).
EXACTLY_ONE_TEST = re.compile(
    r"\.length\s*(?:===|==|!==|!=|>=|<=|>|<)\s*[12]\b"
    r"|\b[12]\s*(?:===|==|!==|!=|>=|<=|>|<)\s*[\w$.\[\]()]*\.length\b")
# The decided prefill as it arrives on an entities row: `admin.service._situation` emits
# `mint_name_prefill` (`entities.situations` decides it), and the console reads it instead of
# deriving one. `row.subjects`/`row.subject` are different keys and do not match.
DECIDED_PREFILL = re.compile(r"\.mint_name_prefill\b")


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


def _balanced(text, start, opener, closer):
    """The substring from `text[start]` (which must be `opener`) through its matching `closer`,
    counting depth and skipping anything inside a string literal — this file's hints and template
    literals contain braces and brackets, and a depth-only scan closes an object early on them."""
    depth = 0
    quote = None
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def _own_keys(obj):
    """`{key: offset}` for the object literal's OWN keys — depth 1 only, so a wrapper does not
    inherit the keys of a descriptor nested inside it."""
    keys, depth, quote, i = {}, 0, None, 0
    while i < len(obj):
        ch = obj[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 1 and obj[:i].rstrip()[-1:] in "{,":
            match = OBJECT_KEY.match(obj, i)
            if match:
                keys[match.group(1)] = match.end()
        i += 1
    return keys


def _confirm_form_field_descriptors(text):
    """Yield `(start_offset, descriptor_text)` for every `confirmForm` FIELD descriptor in `text`
    — an object literal carrying its own `name:` and `label:`, which is the shape `ui.js`'s
    `confirmForm` iterates (`f.name`, `f.label`, `f.value`, `f.kind`, `f.hint`).

    Matched by SHAPE rather than by position inside a `fields: [...]` literal on purpose: two of
    this file's descriptors do not live in one (`actorField()` returns one, `dispatchFlow`
    `push`es one), so a positional scan is blind to exactly the places a new prefill would be
    added next."""
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        obj = _balanced(text, i, "{", "}")
        if obj is None:
            continue
        keys = _own_keys(obj)
        if "name" in keys and "label" in keys:
            yield i, obj


def _value_expression(descriptor):
    """The descriptor's own `value:` expression as source text, or `None` when it has no prefill.
    Ends at the first depth-0 comma or the closing brace, so a value that is a ternary, a call or
    an array literal arrives whole."""
    keys = _own_keys(descriptor)
    if "value" not in keys:
        return None
    depth, quote, out, i = 0, None, [], keys["value"]
    while i < len(descriptor):
        ch = descriptor[i]
        if quote:
            out.append(ch)
            if ch == "\\":
                out.append(descriptor[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
            out.append(ch)
        elif ch in "([{":
            depth += 1
            out.append(ch)
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
            out.append(ch)
        elif ch == "," and depth == 0:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def _function_body(text, name):
    """The source of `function name(...)`'s body, braces included."""
    match = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", text)
    assert match, (
        f"entities.js no longer defines a function named {name} — this check's target moved and it "
        "is now asserting about nothing; repoint it at whatever opens the mint modal")
    brace = text.index("{", text.index(")", match.end()))
    body = _balanced(text, brace, "{", "}")
    assert body, f"{name}'s body did not close — the brace scan lost the file"
    return body


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
    for view in (CAPTURES_VIEW, ENTITIES_VIEW, REPAIRS_VIEW):
        assert view.is_file(), f"{view.name} moved — every check below that reads it by name is blind"


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
    reason NO user, on any input method, can ever reach. The guard scans every shipped file, not
    one card — this exact anti-pattern is a class of bug."""
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


def test_the_console_loads_no_external_resource():
    """Self-contained by CSP AND by construction: no http(s) src/href literal in any shipped file,
    in either attribute spelling, and no `url(https://…)` in the stylesheet — the only absolute
    URLs the app ever renders are GitHub run links built from API data, and those are gated on the
    `https://github.com/` prefix (`test_the_one_external_link_is_gated_on_githubs_own_host`)."""
    offenders = []
    for path in _files(".js", ".html", ".css"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if EXTERNAL_REF.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "external resource reference in the console:\n  " + "\n  ".join(offenders)


def test_the_external_reference_grep_sees_the_object_literal_spelling():
    """The instrument's own specificity, stated: the pattern must catch the `el("a", {href:
    "https://…"})` form this frontend writes, not only the HTML-attribute form it never uses, or
    the ban above is green over the one spelling that could ship."""
    assert EXTERNAL_REF.search('el("a", { href: "https://evil.example/x" })')
    assert EXTERNAL_REF.search("href='http://evil.example'")
    assert EXTERNAL_REF.search("background: url(https://evil.example/a.png)")
    assert not EXTERNAL_REF.search('href: r.html_url'), "a URL from API data is not a literal"


def test_the_one_external_link_is_gated_on_githubs_own_host():
    """The Jobs page renders the Actions run link from an API response. A scheme allowlist is the
    difference between "a link to the logs" and "a link to wherever a compromised response
    says": the anchor is built only behind a `https://github.com/` prefix check."""
    jobs = (STATIC / "assets" / "views" / "jobs.js").read_text(encoding="utf-8")
    anchor = jobs.index("href: r.html_url")
    guard = jobs.rfind('startsWith("https://github.com/")', 0, anchor)
    assert guard != -1 and anchor - guard < 400, (
        "jobs.js renders the run link without the github.com prefix guard immediately before it")


def test_every_confirm_form_states_its_consequence():
    """A button that spends, posts or commits says what it will do before it does: every
    `confirmForm({…})` call site carries a `consequence:` key of its own, and `ui.js` throws on
    an empty one — so a new workflow without a sentence fails loudly in development rather than
    shipping a blank line over Dispatch."""
    ui = (STATIC / "assets" / "ui.js").read_text(encoding="utf-8")
    assert "has no consequence sentence" in ui, "confirmForm no longer refuses an empty consequence"
    missing = []
    for path in sorted((STATIC / "assets" / "views").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\bconfirmForm\(\s*\{", text):
            body = _balanced(text, match.end() - 1, "{", "}") or ""
            if not re.search(r"\bconsequence\s*[:,}]", body):   # `consequence:` or the shorthand
                missing.append(f"{path.name}:{text.count(chr(10), 0, match.start()) + 1}")
    assert not missing, "confirmForm calls with no consequence of their own:\n  " + "\n  ".join(missing)


def test_the_retention_purge_dispatch_defaults_to_a_dry_run():
    """The one workflow that deletes user material: its Run-now form's dry-run box starts TICKED,
    so the default path lists what would go and touches nothing."""
    jobs = (STATIC / "assets" / "views" / "jobs.js").read_text(encoding="utf-8")
    dry_run = re.search(r"""name:\s*["']dry_run["'][^}]*\}""", jobs)
    assert dry_run, "jobs.js no longer declares the dry_run field"
    assert re.search(r"value:\s*true", dry_run.group(0)), (
        "the dry_run checkbox is unticked by default — the default path of the purge dispatch "
        "is the real purge")


def test_admin_console_docs_do_not_promise_an_unreachable_hover_reason():
    """docs/reference/admin-console.md's Captures bullet once promised the three disposition buttons
    are "disabled ... with the reason on hover" — an affordance no browser delivers on a disabled
    control (see the two tests above). Pinned the way `tests/test_readme_claims.py`
    pins README claims against the code: a documented affordance must be checkable against what
    ships, or it rots silently the moment the two drift."""
    doc = (ROOT / "docs" / "reference" / "admin-console.md").read_text(encoding="utf-8")
    start = doc.index("- **Captures**")
    end = doc.index("- **Entities**", start)
    queue_bullet = doc[start:end]
    assert "reason on hover" not in queue_bullet, (
        "docs/reference/admin-console.md's Captures bullet still promises 'reason on hover' — a "
        "disabled button fires no pointer events in any browser, so that tooltip never shows; "
        "once the fix renders the reason as visible inline text, restate this sentence to match")


# ── the repairs detail, once a second kind exists ─────────────────────────────────────────────
# Source-text checks, with the same reach and the same limits the section above states: there is no
# JS runtime here, so what these prove is WHICH DATA the detail view wires in and WHICH CLAIM it
# makes about it — not what a steward sees. That is enough for the one regression that matters,
# because the defect was a SENTENCE: the panel promised every op was additive and nothing was
# rewritten, which stopped being true the day `entity-body` landed.


def test_the_repair_detail_wires_in_the_drafted_body_itself():
    """For an `entity-body` proposal the DRAFT is the whole of what a steward judges. A detail
    view that rendered only `op`/`path`/`link`/`note` would show an empty row where the page's
    new prose should be, and an empty cell reads as "nothing to see"."""
    js = REPAIRS_VIEW.read_text(encoding="utf-8")
    assert "body_markdown" in js, (
        "the console's repair detail never mentions `body_markdown` — the one field an "
        "`entity-body` proposal exists to show a steward")


def test_the_repair_detail_no_longer_promises_that_nothing_is_ever_rewritten():
    """The false sentence, pinned as absent. `entity-body` REPLACES a page's body below its H1,
    so a panel telling a steward "Nothing is rewritten or deleted" beside an Approve button is
    telling them the opposite of what they are authorizing."""
    js = REPAIRS_VIEW.read_text(encoding="utf-8")
    assert "Nothing is rewritten or deleted." not in js, (
        "the repairs detail still claims nothing is ever rewritten — true of the additive kinds "
        "and false of `entity-body`; say it per kind or not at all")


def test_the_repair_detail_renders_a_deletion_as_the_pages_that_would_go():
    """A `delete` proposal's ops are two DIFFERENT shapes — a page removed and a page rewritten —
    and the additive table would render both as edits with an empty "links to" column. The one
    consequence no other kind has is that a page STOPS EXISTING, and it has to be legible as that
    before anybody presses Approve."""
    js = REPAIRS_VIEW.read_text(encoding="utf-8")
    assert "deletionPlan" in js, (
        "the console's repair detail has no renderer for a deletion — the third kind would be "
        "shown as a table of additive edits with two empty columns")
    assert "KIND_DELETE" in js


def test_the_additive_summary_still_says_nothing_is_deleted_only_for_the_additive_kinds():
    """The same defect as the `entity-body` sentence, one kind later: the per-kind consequence
    line is the last thing a steward reads before Approve, and a deletion falling through to the
    additive branch would tell them nothing is deleted while deleting a page."""
    js = REPAIRS_VIEW.read_text(encoding="utf-8")
    start = js.index("function changeSummary")
    summary = js[start:js.index("\n}\n", start)]
    assert "KIND_DELETE" in summary, (
        "`changeSummary` does not branch on the delete kind, so a deletion is described by "
        "whichever sentence happens to be the fallback")
    assert summary.index("KIND_DELETE") < summary.index("Nothing is rewritten or deleted here"), (
        "the additive sentence is reached before the delete branch, so a deletion would be "
        "described as a change that deletes nothing")


# ── the theme: three states, stamped before the first paint ────────────────────────────────────
def test_the_theme_is_stamped_by_a_classic_script_before_the_module_graph():
    """A chosen dark theme must not flash light on every load. A module is deferred until the
    document is parsed, and the console's CSP (`script-src 'self'`) refuses an inline script — so
    the early stamp is a classic script file, and it has to come BEFORE `app.js` in the head."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert (STATIC / "assets" / "theme.js").is_file(), "theme.js is not shipped"
    theme_at = html.index('src="./assets/theme.js"')
    app_at = html.index('src="./assets/app.js"')
    assert theme_at < app_at, "theme.js loads after app.js — the stamp arrives too late to matter"
    assert 'type="module"' not in html[html.rindex("<script", 0, theme_at):theme_at], (
        "theme.js is loaded as a module, so it is deferred and the flash is back")


def test_the_early_stamp_and_the_picker_agree_on_the_storage_key_and_the_state_names():
    """`theme.js` (classic, cannot be imported) and `ui.js` (the picker) each spell the key and
    the two stamped state names. A drift between them is silent: the picker would write a
    preference the early stamp never reads, so the choice would only take effect after the module
    graph loaded — the flash it exists to prevent, back for the one steward who chose."""
    early = (STATIC / "assets" / "theme.js").read_text(encoding="utf-8")
    ui = (STATIC / "assets" / "ui.js").read_text(encoding="utf-8")
    assert '"stigmergy-ops-theme"' in early and '"stigmergy-ops-theme"' in ui, (
        "the storage key is spelled differently in theme.js and ui.js")
    for state in ('"light"', '"dark"'):
        assert state in early and state in ui, f"{state} is not handled on both sides"
    assert 'setAttribute("data-theme"' in early and 'setAttribute("data-theme"' in ui, (
        "the two sides stamp different attributes")


def test_every_colour_token_carries_both_themes():
    """The tokens are declared once as `light-dark(light, dark)`, which is what makes a token
    added to one theme and forgotten in the other impossible. A bare colour in `:root` is that
    forgetting — it would render identically in both themes, invisibly, until somebody opened the
    console in the other one. (`--accent-ink` is white in both by design, and the fallback block
    under `@supports not` is the light palette on purpose.)"""
    css = (STATIC / "assets" / "styles.css").read_text(encoding="utf-8")
    root = css[css.index(":root {"):css.index("/* The three states.")]
    allowed_single = {"--accent-ink"}
    offenders = []
    for line in root.splitlines():
        name, _, value = line.strip().partition(":")
        if not name.startswith("--") or name in allowed_single:
            continue
        looks_like_colour = re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", value)
        if looks_like_colour and "light-dark(" not in value and "var(--" not in value:
            offenders.append(line.strip())
    assert not offenders, (
        "a colour token is declared for one theme only — say it as `light-dark(light, dark)` so "
        "the pair cannot drift:\n  " + "\n  ".join(offenders))


def test_the_theme_states_are_the_three_the_picker_offers():
    """Auto stamps nothing (the OS decides through `color-scheme: light dark`); the two explicit
    states pin the scheme, which is what lets a chosen LIGHT beat an OS in dark mode. All three
    have to exist in the stylesheet, or a picker button would do nothing."""
    css = (STATIC / "assets" / "styles.css").read_text(encoding="utf-8")
    assert "color-scheme: light dark;" in css, "Auto has no rule — nothing follows the OS"
    assert ':root[data-theme="light"] { color-scheme: light; }' in css
    assert ':root[data-theme="dark"] { color-scheme: dark; }' in css
    assert "@supports not (color: light-dark(" in css, (
        "no fallback: a browser without light-dark() would render every token invalid")


def test_each_detail_route_parses_its_own_id_and_the_entity_route_keeps_the_slug():
    """A proposal's id is a registry slug (`acme-corp`), a capture's and a repair's a row number.
    The router once coerced EVERY detail segment with `Number(...)`, so the first click on a
    proposal in the inbox asked the API for `entities/NaN` and met a 404 — the one defect a
    Python test over the routes could never see. Each route now names its own parser, and the
    entity one must not be numeric."""
    app = (STATIC / "assets" / "app.js").read_text(encoding="utf-8")
    routes = re.findall(r"\{ pattern: /\^(\w+)\\/.*?, id: (\w+) \}", app)
    assert dict(routes) == {"captures": "Number", "entities": "decodeURIComponent", "repairs": "Number"}, routes
    assert "Number(match[1])" not in app
    assert "d.id(match[1])" in app
