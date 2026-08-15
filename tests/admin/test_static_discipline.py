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
        f"views.js no longer defines a function named {name} — this check's target moved and it "
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
    reason NO user, on any input method, can ever reach (the queue detail card's three
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
    control (see the two tests above). Pinned the way `tests/test_readme_claims.py`
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


# ── the mint modal's `Name` prefill: what a steward's accepted default MINTS ────────────────────
# The console's OTHER mint door. Submitting `entityApproveFlow`'s modal is, in that modal's own
# words, "ONE commit to the knowledge repo … Not something cancelling after this point can undo",
# so whatever sits in the `Name` field is in practice what gets minted — a prefill is the value
# most stewards submit unchanged. The rule is the Slack surface's, pinned there on a real rendered
# payload (`tests/slack/test_render.py`, `render_entity_mint_modal`): prefill only when there is
# exactly ONE unresolved name; with several, no single string is the right answer, so the field
# stays empty and the names are listed for the steward to choose from.
#
# WHAT THIS INSTRUMENT PROVES, AND WHAT IT CANNOT. These four tests read the SOURCE TEXT of a
# shipped frontend asset. There is no JS runtime in this suite, so they cannot open the modal,
# cannot see what a steward is shown, and cannot prove the submitted value. They prove exactly
# two things — that no field prefill is wired to the joined display string, and that the mint
# flow's prefill is still there and still guarded by an exactly-one condition — and they would
# stay green against a fix that read the right data and still rendered the wrong field. That gap
# is not closable from Python: it is the seam the developer is asked to add (a pure, exported
# prefill decision — or, better, one server-side field both mint doors read), reported alongside
# these tests rather than papered over by them.
def test_no_confirm_form_prefill_is_wired_to_the_joined_subject_display_string():
    """**The regression test.** `entities.situations.subject_of` returns ONE display string: for a
    park naming two unresolved entities it is `"Jack, Acme Capital"`, the two names joined with a
    comma. `entityApproveFlow` prefilled `Name` from it, so a steward accepting that prefill —
    which is what a prefill is for — minted a real entity called "Jack, Acme Capital" and pushed a
    real signed commit for it. It is neither of the two names, no registry lookup will ever match
    it, and undoing it is a second commit. `admin.service._situation` already emits `subjects`,
    the per-name list, beside `subject`; only this file ignored it.

    Scoped to `value:` and to every `confirmForm` field descriptor in the file, not to one line:
    the same mistake made in a different flow, or in a descriptor a helper returns, fails here
    too. `label:`/`hint:` are deliberately NOT scanned — showing a steward the joined string as
    read-only context is a legitimate display use (`entityDetailView`'s `kv` table does it); only
    what lands in an INPUT the steward submits is the defect."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    descriptors = list(_confirm_form_field_descriptors(views))
    assert len(descriptors) >= 5, (
        f"only {len(descriptors)} confirmForm field descriptors found in views.js — the form "
        "shape changed and this scan went blind; repoint it before trusting a green run")
    assert any(_value_expression(d) for _, d in descriptors), (
        "no descriptor carries a `value:` at all — either prefills moved out of the descriptor "
        "shape, or `_value_expression` stopped parsing them; this test is asserting on nothing")
    offenders = []
    for start, descriptor in descriptors:
        expression = _value_expression(descriptor)
        if expression and JOINED_SUBJECT.search(expression):
            line = views.count("\n", 0, start) + 1
            name = re.search(r"""name\s*:\s*["']([^"']+)""", descriptor)
            offenders.append(f"views.js:{line}: field {name.group(1) if name else '?'} — "
                             f"value: {expression}")
    assert not offenders, (
        "a form field prefills from the JOINED subject display string — for a park naming several "
        "unresolved entities that string is the names glued together with commas, and a steward "
        "who accepts it mints ONE garbled entity with ONE irreversible commit; read `subjects` "
        "(the per-name list the backend already sends) and prefill only when it holds exactly "
        "one name, the way slack/render.py::render_entity_mint_modal does:\n  "
        + "\n  ".join(offenders))


def test_the_mint_flow_never_reaches_for_the_joined_subject_at_all():
    """The alias hole the test above cannot see: a `const proposed = row.subject` hoisted out of
    the descriptor, then passed as `value: proposed`, is the identical defect with a value
    expression that mentions no `subject`. `entityApproveFlow` renders no table and no read-only
    context — it collects the metadata a mint writes — so inside THIS function the joined display
    string has no legitimate reader at all, and banning the whole name is both simpler and
    stricter than tracing the assignment."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    body = _function_body(views, "entityApproveFlow")
    assert "confirmForm" in body, (
        "entityApproveFlow no longer opens a confirmForm — the mint door moved and this check is "
        "guarding the wrong function")
    assert not JOINED_SUBJECT.search(body), (
        "entityApproveFlow still reads the joined `.subject` display string; every value this "
        "function collects is submitted to the mint, so the per-name `subjects` list is the only "
        "correct source here:\n  "
        + "\n  ".join(line.strip() for line in body.splitlines()
                      if JOINED_SUBJECT.search(line)))


def test_the_mint_name_field_still_carries_a_prefill():
    """**The benign twin**, and it is green both before and after the fix: it goes red only on the
    over-broad repair. Blanking `Name` unconditionally would also make the two tests above pass,
    while trading a rare garbled mint for a retyped name on EVERY approval — and a steward who
    retypes the same name every time learns to stop reading the field, which is how the next
    wrong value gets submitted. The common case is one unresolved name, and it must keep its
    default."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    body = _function_body(views, "entityApproveFlow")
    descriptors = [d for _, d in _confirm_form_field_descriptors(body)
                   if re.search(r"""name\s*:\s*["']name["']""", d)]
    assert len(descriptors) == 1, (
        f"expected exactly one `name` field descriptor in entityApproveFlow, found "
        f"{len(descriptors)} — the mint form was restructured; repoint this check")
    expression = _value_expression(descriptors[0])
    assert expression, (
        "the mint form's `Name` field no longer carries a `value:` at all — the prefill was "
        "deleted rather than made conditional, so a steward now retypes the name on every single "
        "approval including the one-name case that is the overwhelming majority")
    assert expression.strip(" ()") not in ('""', "''", "``"), (
        f"the mint form's `Name` prefill is the constant empty string ({expression}) — that is "
        "the over-broad fix: it blanks the one-name case too. Prefill when `subjects` holds "
        "exactly one name, empty only when it holds several or none")


def test_the_mint_prefill_is_decided_by_how_many_unresolved_names_there_are():
    """The other half of the contract, and the one an empty-field fix would skip: the prefill must
    be a DECISION over the per-name list, not an unconditional read of its first element.
    `subjects[0]` alone is the same defect wearing the right field name — it prefills "Jack" for a
    park about Jack AND Acme Capital, and a steward who accepts it mints one of the two and
    silently drops the other, which the joined string at least made visible.

    A source-text proxy, honestly: it asserts the flow reads `subjects` and branches on a length
    against 1 or 2, in whichever direction the fix writes it. It cannot prove the branch is the
    right way round, and it cannot prove the several-names case lists the names for the steward
    the way the Slack modal does — see this section's header for the seam that would."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    body = _function_body(views, "entityApproveFlow")
    assert re.search(r"\bsubjects\b", body), (
        "entityApproveFlow never reads `subjects` — the per-name list `admin.service._situation` "
        "already sends beside the joined `subject`, and the only source that can tell one "
        "unresolved name from several")
    assert EXACTLY_ONE_TEST.search(body), (
        "entityApproveFlow reads `subjects` but branches on nothing: no comparison of its length "
        "against one. Prefilling `subjects[0]` unconditionally mints the first of several names "
        "and drops the rest — with more than one name the field must stay EMPTY and the names be "
        "listed, as slack/render.py::render_entity_mint_modal does")
