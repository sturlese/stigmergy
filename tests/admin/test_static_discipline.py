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
# most stewards submit unchanged.
#
# The rule — prefill only when exactly ONE unresolved name could be meant; with several, no single
# string is right, so the field stays empty and the names are listed — is no longer written in this
# file, nor in the Slack renderer that used to state its own copy. It is decided once in
# `entities.situations.mint_name_prefill`, pinned there directly on the pure function
# (`tests/entities/test_situations.py`), and delivered as `mint_name_prefill` on both entity routes
# (`tests/admin/test_routes_pg.py`) and on the review item the Slack door reads
# (`tests/server/test_review.py`). That is the seam this section used to ask for, and it exists.
#
# WHAT THIS INSTRUMENT PROVES, AND WHAT IT CANNOT. Every test in this section reads the SOURCE TEXT
# of a shipped frontend asset. There is no JS runtime in this suite, so none of them can open the
# modal, see what a steward is shown, or prove the submitted value. What they can prove is WHICH
# DATA this file wires into the mint form: that no prefill is wired to the joined display string,
# that the `Name` prefill is the decided field and not a local re-derivation, and that the
# several-names listing is still built. They would stay green against a change that read the right
# field and rendered it in the wrong place — that residue is the untestable-from-Python part, and
# it is smaller than it was, because the value's correctness is now provable where it is decided
# instead of only inferable from a grep.
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


def test_the_mint_flow_reads_the_per_name_list_and_never_counts_it_again():
    """RETARGETED, and the direction of the change is the whole point.

    It used to REQUIRE a length comparison in this function: back then the prefill was decided
    here, and a flow reading `subjects` without branching on its size was one that prefilled
    `subjects[0]` for a park about Jack AND Acme Capital. The decision now lives in
    `entities.situations.mint_name_prefill` and arrives as a field
    (`tests/entities/test_situations.py` pins the rule, `tests/admin/test_routes_pg.py` pins its
    delivery on both entity routes), so the same assertion has INVERTED: a count comparison
    reappearing in this function is a second policy, and two policies over one irreversible mint
    drift — which is the defect the consolidation removed.

    The `subjects` half is unchanged and now guards the LISTING: with no default to offer, the
    names still have to be enumerated for the steward, and `subjects` is the only source for that
    (`test_the_several_names_case_still_lists_the_names_for_the_steward` pins the enumeration
    itself). What the ban catches is a count compared against ONE, not every read of `.length`:
    the banner is gated on `!proposed && names.length` — the server's decision, plus "is there
    anything left to list" — which is a non-empty test and correctly does not match. The banner's
    own sentence states no number at all, deliberately: the decision was taken on the raw row
    while these bullets are the post-`_clean` survivors, so any count named there could contradict
    the list it introduces on exactly the park the rule exists for."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    body = _function_body(views, "entityApproveFlow")
    assert re.search(r"\bsubjects\b", body), (
        "entityApproveFlow never reads `subjects` — the per-name list `admin.service._situation` "
        "sends beside the joined `subject`, and the only source the several-names listing can be "
        "built from")
    offenders = [line.strip() for line in body.splitlines() if EXACTLY_ONE_TEST.search(line)]
    assert not offenders, (
        "entityApproveFlow compares a length against one again — the one-vs-several rule is "
        "decided ONCE, in `entities.situations.mint_name_prefill`, and reaches this flow as "
        "`row.mint_name_prefill`; a second derivation here is a second policy over the same "
        "irreversible mint, and the two doors drift the moment either changes:\n  "
        + "\n  ".join(offenders)
        + "\nIf the decision is deliberately moving back into the browser, this test has done its "
        "job by failing: say so, and move the Python proofs with it.")


# The two below scope themselves to the `Name` descriptor's own `value:` expression, following it
# through its `const` binding, rather than scanning the whole function the way the greps above do:
# a body-wide scan is satisfied by anything anywhere in the flow, including the several-names
# banner, so it cannot see what actually reaches the input a steward submits.
#
# THE DECISION MOVED, and these went red saying so — which is what they were for. Before the
# consolidation they required the `Name` value expression to compare a name COUNT; the prefill is
# now decided server-side, so they require it to be the decided FIELD instead, and the count
# comparison is banned rather than demanded. Repointing them was a deliberate act with a new source
# of truth to name (`entities.situations.mint_name_prefill`) and Python proofs landing beside it
# (the pure function, both wires, the Slack modal). What must never happen is the decision moving
# and this file staying quietly green — the failure that stopped that is on the record.
def _resolved_value_expression(body, expression):
    """`expression`, or — when it is a bare identifier bound in `body` — the initializer it is
    bound to. One hop only: a prefill hidden behind two levels of aliasing is not something a
    source scan should chase, and the assertion message says so."""
    identifier = re.fullmatch(r"[A-Za-z_$][\w$]*", expression.strip())
    if not identifier:
        return expression
    binding = re.search(r"\b(?:const|let|var)\s+" + re.escape(expression.strip()) + r"\s*=",
                        body)
    if not binding:
        return expression
    depth, quote, out, i = 0, None, [], binding.end()
    while i < len(body):
        ch = body[i]
        if quote:
            out.append(ch)
            if ch == "\\":
                out.append(body[i + 1])
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
            depth -= 1
            out.append(ch)
        elif ch == ";" and depth == 0:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def test_the_name_prefills_own_expression_is_the_decided_field_and_nothing_else():
    """RETARGETED (see the block above). The `Name` field's `value:` — not the function around it —
    is what a steward submits, so it is where the source of the prefill has to be visible. It used
    to have to resolve to `names.length === 1 ? names[0] : ""`; it now has to resolve to a read of
    `row.mint_name_prefill`, the value `entities.situations` decided and `admin.service._situation`
    only sanitized and sent, and to nothing that decides again.

    Both halves are needed and neither is redundant. Without the first, `value:` could become
    `subjects[0]` — the C-3 defect wearing the right field name — and the sibling greps would stay
    green, since they only ban the joined `subject` and only ask that `subjects` be read somewhere.
    Without the second, the flow could read the decided field and then override it with its own
    count, which is the duplicate policy this whole change removed.

    The key string is asserted against the shaper that emits it, so a rename on either side fails
    here instead of silently handing the browser `undefined` — a prefill that quietly becomes empty
    for every park, with no listing either, is invisible from this side of the wire."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    body = _function_body(views, "entityApproveFlow")
    descriptors = [d for _, d in _confirm_form_field_descriptors(body)
                   if re.search(r"""name\s*:\s*["']name["']""", d)]
    assert len(descriptors) == 1, (
        f"expected exactly one `name` field descriptor in entityApproveFlow, found "
        f"{len(descriptors)} — the mint form was restructured; repoint this check")
    expression = _value_expression(descriptors[0])
    resolved = _resolved_value_expression(body, expression or "")
    assert DECIDED_PREFILL.search(resolved), (
        f"the `Name` prefill expression ({expression!r} -> {resolved!r}) is not the decided "
        "`row.mint_name_prefill` the API sends. Submitting this field mints one entity with one "
        "irreversible commit, and the one-vs-several rule that says whether a default is safe at "
        "all lives in `entities.situations.mint_name_prefill` — a prefill built from anything else "
        "here is either the C-3 joined compound or a second copy of that rule.\n"
        "If the decision deliberately moved again, this test has done its job by failing: repoint "
        "it at the new source of truth and move the Python proofs "
        "(tests/entities/test_situations.py, tests/admin/test_routes_pg.py) with it.")
    assert not EXACTLY_ONE_TEST.search(resolved), (
        f"the `Name` prefill expression ({expression!r} -> {resolved!r}) reads the decided field "
        "AND compares a name count of its own — the browser is overriding, or second-guessing, the "
        "one decision. One of the two wins on some input, and nobody knows which without reading "
        "this expression, which is exactly the state the consolidation ended")
    service = (ROOT / "src" / "stigmergy" / "admin" / "service.py").read_text(encoding="utf-8")
    assert "mint_name_prefill" in service, (
        "views.js prefills from `row.mint_name_prefill` but `admin/service.py` never mentions that "
        "key — the shaper renamed or dropped it and the browser now reads `undefined`, which "
        "prefills nothing AND lists nothing: an unexplained empty required field on every approval")


def test_the_several_names_case_still_lists_the_names_for_the_steward():
    """The half of the rule the prefill greps above do not check at all: an empty required field
    with no explanation is a riddle, so with several unresolved names the console must SHOW them —
    `tests/slack/test_render.py` pins exactly this for the other door on a real payload. Here it
    can only be a proxy: the flow builds one list item per name from the per-name list.

    It reds if the listing is dropped while the prefill is preserved, which is the plausible
    accident now that this function reads a decided value and needs `names` for nothing else."""
    views = (STATIC / "assets" / "views.js").read_text(encoding="utf-8")
    body = _function_body(views, "entityApproveFlow")
    assert re.search(r"\.map\s*\(", body), (
        "entityApproveFlow no longer iterates anything — with several unresolved names the steward "
        "is shown an empty required `Name` field and no indication of which names are waiting")
    assert re.search(r"""["']li["']""", body), (
        "entityApproveFlow builds no list items — the several-names case must still enumerate the "
        "names for the steward, the way the Slack mint modal lists them above the empty field")


# ── the repairs detail, once a second kind exists ─────────────────────────────────────────────
# Source-text checks, with the same reach and the same limits the section above states: there is no
# JS runtime here, so what these prove is WHICH DATA the detail view wires in and WHICH CLAIM it
# makes about it — not what a steward sees. That is enough for the one regression that matters,
# because the defect was a SENTENCE: the panel promised every op was additive and nothing was
# rewritten, which stopped being true the day `entity-body` landed.
REPAIRS_VIEW = STATIC / "assets" / "views.js"


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
