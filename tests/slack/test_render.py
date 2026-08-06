"""`stigmergy.slack.render` — the pure `(answer_dict, link_resolver) -> blocks` function. No Slack,
no network: every test drives it with a hand-built `answer` dict, the same shape
`AnswerService.ask()` returns.
"""
import json

import pytest

from stigmergy.slack import copy, render


def _text(blocks: list[dict]) -> str:
    """Every mrkdwn text string across a block list, concatenated — the cheap way to assert "this
    string appears somewhere" without caring which block it landed in. Pulls from BOTH `section`
    blocks (the answer body) and `context` blocks (the Sources block and the verdict line, which
    render as `context` so Slack draws them smaller and grey — a channel the body cannot
    imitate)."""
    parts = []
    for b in blocks:
        if b.get("type") == "section":
            parts.append(b["text"]["text"])
        elif b.get("type") == "context":
            parts.extend(e["text"] for e in b.get("elements", []) if e.get("type") == "mrkdwn")
    return "\n".join(parts)


VERIFIED_ANSWER = {
    "question": "what is Acme's ARR?", "refused": False,
    "answer_markdown": "Acme's **ARR** is 512000 usd.",
    "reason": "", "citations": [{"path": "wiki/notes/acme.md", "quote": "ARR reached 512000"}],
    "confidence": "high", "verdict": {"verdict": "verified", "unverified_figures": [],
                                     "citation_problems": []},
    "retried": False, "suppressed": False, "built_at": "2026-07-28T00:00:00Z",
}

PARTIAL_ANSWER = {**VERIFIED_ANSWER,
                  "verdict": {"verdict": "partial", "unverified_figures": [],
                             "citation_problems": ["citation quote not found"]}}

REFUSED_ANSWER = {
    "question": "what is Globex's ARR?", "refused": True, "answer_markdown": "",
    "reason": "no pages matched: what is Globex's ARR?", "citations": [], "confidence": "low",
    "verdict": {"verdict": "failed", "unverified_figures": [], "citation_problems": []},
    "retried": False, "suppressed": False, "built_at": None,
}


def _no_link(path: str) -> str | None:
    return None


def _all_link(path: str) -> str | None:
    return f"https://read.example.com/{path}"


def _fake_mint_token(path: str, asker_slack_user_id: str) -> str:
    """A test double for `SlackContext.mint_show_it_here_token` — `render.py` never touches a
    token STORE itself, so this just proves the two arguments it is CALLED with, with no server-
    side state needed to test that."""
    return f"TOKEN::{path}::{asker_slack_user_id}"


# ── the verdict — never flattened ──────────────────────────────────────────────────────────────
def test_verified_answer_renders_the_verified_trust_line():
    blocks = render.render_answer(VERIFIED_ANSWER, _no_link)
    assert copy.VERDICT_LINES["verified"] in _text(blocks)
    assert copy.VERDICT_LINES["partial"] not in _text(blocks)


def test_partial_answer_renders_the_partial_trust_line_never_the_verified_one():
    blocks = render.render_answer(PARTIAL_ANSWER, _no_link)
    assert copy.VERDICT_LINES["partial"] in _text(blocks)
    assert copy.VERDICT_LINES["verified"] not in _text(blocks)


def test_an_unrecognized_verdict_raises_rather_than_silently_flattening():
    """A missing branch must throw. `failed` never reaches this function in
    production (AnswerService always suppresses it into a refusal first) — this proves the
    renderer does not paper over that invariant if it were ever violated."""
    bogus = {**VERIFIED_ANSWER, "verdict": {"verdict": "failed", "unverified_figures": [],
                                            "citation_problems": []}}
    with pytest.raises(KeyError):
        render.render_answer(bogus, _no_link)


# ── confidence is never rendered (developer ruling 6) ────────────────────────────────────────────
def test_confidence_field_never_appears_in_the_rendered_blocks():
    blocks = render.render_answer(VERIFIED_ANSWER, _no_link)
    rendered = json.dumps(blocks)
    assert "confidence" not in rendered
    assert VERIFIED_ANSWER["confidence"] not in rendered


# ── the refusal — an answer, not an error ────────────────────────────────────────────────────────
def test_refusal_renders_the_bold_lead_and_the_reason_verbatim():
    blocks = render.render_answer(REFUSED_ANSWER, _no_link)
    text = _text(blocks)
    assert text.startswith("*I don't have that.*")
    assert REFUSED_ANSWER["reason"] in text


def test_refusal_never_uses_the_word_refused():
    blocks = render.render_answer(REFUSED_ANSWER, _no_link)
    assert "refused" not in _text(blocks).lower()


# ── citations: the no-link contract — there is no read site, so the ONLY resolver production
# wires is `slack.settings.no_link_resolver` ───────────────────────────────────────────────────
def test_a_citation_gets_the_affordance_and_no_link_under_the_no_link_resolver():
    blocks = render.render_answer(VERIFIED_ANSWER, _no_link, asker_slack_user_id="U_ANA",
                                  mint_token=_fake_mint_token)
    text = _text(blocks)
    assert "not on the read site yet" in text
    assert "https://" not in text
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 1
    button = actions[0]["elements"][0]
    assert button["text"]["text"] == copy.SHOW_IT_HERE_LABEL
    assert button["action_id"] == render.SHOW_IT_HERE_ACTION_ID
    # the value is an OPAQUE token — never the path or the asker's email in cleartext.
    assert button["value"] == "TOKEN::wiki/notes/acme.md::U_ANA"
    assert "asker_email" not in button["value"]
    assert "ana@example.com" not in button["value"]


def test_the_renderer_still_calls_the_resolver_seam_so_a_future_resolver_can_render_a_link():
    """There is no read site, but `render_answer`'s `link_resolver` parameter is still a real
    seam, not a dead one — a future browsable surface wires its own resolver in at
    `stigmergy.slack.app.build_context`, with no change to this module's contract. Proven here
    by a resolver that DOES return a URL: the renderer must still use it, and must not render the
    affordance button when it does."""
    blocks = render.render_answer(VERIFIED_ANSWER, _all_link, asker_slack_user_id="U_ANA",
                                  mint_token=_fake_mint_token)
    text = _text(blocks)
    assert "<https://read.example.com/wiki/notes/acme.md|" in text
    assert not any(b.get("type") == "actions" for b in blocks)


def test_no_citations_produces_no_sources_block():
    answer = {**VERIFIED_ANSWER, "citations": []}
    blocks = render.render_answer(answer, _no_link)
    assert "Sources" not in _text(blocks)


# ── two citations of the SAME page must not collide on block_id ────────────────────────────────
# `stigmergy.answer.synthesize.Citation` carries no uniqueness constraint on `path`, and
# `AnswerService._shape` ships `out.citations` straight through with no dedup
# (`[{"path": c.path, "quote": c.quote} for c in out.citations]`) — a real model citing one page
# twice, with two different quotes, is a LEGAL `answer` shape, exactly what was observed live on
# staging. Before the fix, `_citation_blocks` built one "Show it here" actions block PER CITATION
# with `block_id = f"show_it_here:{path}"` (`render.py`, pre-fix) — two citations of one page
# produced two actions blocks sharing the SAME block_id, which Slack's real API rejects outright
# (`invalid_blocks`, "block_id show_it_here:<path> already exists" — mirrored by
# `gateway.FakeSlackGateway`'s now-unconditional block_id-uniqueness check).
DUPLICATE_CITATION_ANSWER = {
    **VERIFIED_ANSWER,
    "answer_markdown": "Acme's **ARR** is 512000 usd, confirmed in two places on the same page.",
    "citations": [
        {"path": "wiki/notes/acme.md", "quote": "ARR reached 512000 in Q1"},
        {"path": "wiki/notes/acme.md", "quote": "ARR reached 512000 again in the Q2 recap"},
    ],
}


def test_two_citations_of_the_same_page_render_one_button_with_unique_opaque_block_ids():
    """The reproduction at the render level. The fix: one "Show it here" button per DISTINCT page
    (dedupe by path, restoring block_id uniqueness), an OPAQUE block_id (no page path in it —
    symmetry with the button's own `value` token, which `_show_it_here_button`'s docstring already
    requires to be opaque), and BOTH citation quotes still listed as separate Sources lines — only
    the BUTTON collapses per page, never the citation text. Fails today: two actions blocks, both
    named `show_it_here:wiki/notes/acme.md` (a real, reproducible `invalid_blocks` collision, not
    merely a hypothetical one)."""
    blocks = render.render_answer(DUPLICATE_CITATION_ANSWER, _no_link, asker_slack_user_id="U_ANA",
                                  mint_token=_fake_mint_token)

    block_ids = [b["block_id"] for b in blocks if "block_id" in b]
    assert len(block_ids) == len(set(block_ids)), f"duplicate block_id(s) in {block_ids}"

    actions_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions_blocks) == 1, "exactly one button for the one DISTINCT cited page"
    assert len(actions_blocks[0]["elements"]) == 1

    for block_id in block_ids:
        assert "wiki/notes/acme.md" not in block_id, (
            f"block_id must be opaque — no page path in it, got {block_id!r}")

    text = _text(blocks)
    assert "ARR reached 512000 in Q1" in text
    assert "ARR reached 512000 again in the Q2 recap" in text


TWO_DISTINCT_CITATIONS_ANSWER = {
    **VERIFIED_ANSWER,
    "answer_markdown": "Acme's ARR was 512000 usd, and headcount was 40.",
    "citations": [
        {"path": "wiki/notes/acme.md", "quote": "ARR reached 512000"},
        {"path": "wiki/notes/acme-headcount.md", "quote": "headcount stood at 40"},
    ],
}


def test_two_citations_of_different_pages_render_two_buttons_with_distinct_opaque_block_ids():
    """Symmetry check for decided-fix (b), isolated from any deduplication: block_id opacity is
    stated as a general rule ("symmetry with the value-token rule in slack.md"), not merely a
    side-effect of collapsing duplicate paths — so it must hold here too, where there is nothing
    to dedupe (two DIFFERENT cited pages). Proves two things dedup-by-path must NOT do: collapse
    to one button regardless of path (still two buttons, one per distinct page), or leak either
    path into its own block_id. Each button also stays scoped to its OWN page via the (unchanged)
    opaque `value` token. Fails today: block_id is literally `f"show_it_here:{path}"` — the path
    is IN the id, for both pages."""
    blocks = render.render_answer(TWO_DISTINCT_CITATIONS_ANSWER, _no_link,
                                  asker_slack_user_id="U_ANA", mint_token=_fake_mint_token)
    actions_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions_blocks) == 2, "one button per DISTINCT cited page — dedup must not over-collapse"

    block_ids = [b["block_id"] for b in actions_blocks]
    assert len(block_ids) == len(set(block_ids))
    for block_id in block_ids:
        assert "wiki/notes/acme.md" not in block_id
        assert "wiki/notes/acme-headcount.md" not in block_id

    values = {b["elements"][0]["value"] for b in actions_blocks}
    assert values == {"TOKEN::wiki/notes/acme.md::U_ANA", "TOKEN::wiki/notes/acme-headcount.md::U_ANA"}


# ── the DM fuller answer wrapper ──────────────────────────────────────────────────────────────────
def test_dm_fuller_answer_states_the_original_channel_and_question():
    blocks = render.render_dm_fuller_answer(channel_name="finance", question="what is Acme's ARR?",
                                            answer=VERIFIED_ANSWER, link_resolver=_no_link)
    text = _text(blocks)
    assert "#finance" in text
    assert "what is Acme's ARR?" in text
    assert "fuller answer" in text


# ── the small single-purpose renders — string identity against the ux spec ─────────────────────
def test_no_access_channel_and_dm_copy_differ_only_as_specified():
    channel = render.render_no_access(is_dm=False)[0]["text"]["text"]
    dm = render.render_no_access(is_dm=True)[0]["text"]["text"]
    assert channel == copy.NO_ACCESS_CHANNEL
    assert dm == copy.NO_ACCESS_DM
    assert channel != dm


def test_capture_ack_names_the_reactor_and_never_claims_it_is_already_saved_or_searchable():
    """The ack legitimately says "you'll hear back... when it's filed" (future tense —
    that IS the push-channel promise); what it must never claim is that the material already IS
    saved, filed or searchable right now."""
    text = render.render_capture_ack("Ana")[0]["text"]["text"]
    assert "Ana" in text
    assert "when it's filed" in text   # the honest future-tense promise, present on purpose
    for forbidden in ("saved", "searchable", "is filed", "has been filed", "was filed"):
        assert forbidden not in text.lower()


def test_filed_render_states_the_freshness_gap_without_naming_internal_tools():
    text = render.render_filed(page_path="wiki/entities/Acme.md", commit="abc123",
                               anchor="Acme Corp")[0]["text"]["text"]
    assert "`wiki/entities/Acme.md`" in text
    assert "`abc123`" in text
    assert "Acme Corp" in text
    assert "search_brain" not in text and "ask(" not in text


def test_needs_input_render_addresses_the_submitter_and_never_shows_the_mcp_invocation():
    text = render.render_needs_input(situation_prose="needs_input — parked on one question",
                                     slack_user_id="U123")[0]["text"]["text"]
    assert text.startswith("<@U123> —")
    assert copy.NEEDS_INPUT_INSTRUCTION in text
    assert "brain_reply(" not in text


def test_generic_report_bolds_the_enum_prefix_and_keeps_the_rest_of_the_sentence():
    raw = "triage — parked, not filed. Your material seems to be about \"X\"."
    text = render.render_generic_report("triage", raw)[0]["text"]["text"]
    assert text.startswith("*triage* — parked, not filed.")
    assert "triage — triage" not in text   # never a duplicated prefix


# ── the Sources block and the verdict line are the bot's OWN structure, and a prompt-injected
# answer body must not be able to imitate them ─────────────────────────────────────────────────
def test_the_real_sources_block_and_verdict_line_render_as_context_blocks_not_sections():
    """`escape_mrkdwn` only escapes `&`/`<`/`>` — asterisks, headers and newlines survive in
    `answer_markdown`. The structural defense is that the REAL Sources block and verdict line
    render as `context` blocks (smaller, grey — Slack's own chrome), a channel the answer BODY
    (always a `section` block) cannot reach into, no matter what it contains."""
    blocks = render.render_answer(VERIFIED_ANSWER, _no_link, asker_slack_user_id="U_ANA",
                                  mint_token=_fake_mint_token)
    section_blocks = [b for b in blocks if b.get("type") == "section"]
    context_blocks = [b for b in blocks if b.get("type") == "context"]
    divider_blocks = [b for b in blocks if b.get("type") == "divider"]

    assert len(section_blocks) == 1   # the answer body, and ONLY the answer body
    assert context_blocks, "the Sources block and the verdict line must render as context blocks"
    assert divider_blocks, "a divider must separate the answer body from the bot's own chrome"

    context_text = "\n".join(e["text"] for b in context_blocks for e in b["elements"])
    assert "Sources" in context_text
    assert copy.VERDICT_LINES["verified"] in context_text


def test_an_injected_fake_sources_header_and_verdict_in_the_answer_body_cannot_impersonate_the_real_chrome():
    """A prompt-injected page could steer the agent into emitting, inside `answer_markdown` itself,
    a forged `*Sources*` header, a citation-shaped bullet, and a literal verified-verdict sentence
    — visually indistinguishable from the real ones IF everything renders as `section` blocks. This
    proves the forged text stays confined to the one `section` block (the body), while the REAL
    Sources/verdict remain `context` blocks — distinguishable by BLOCK TYPE regardless of content."""
    injected = {**VERIFIED_ANSWER,
               "answer_markdown": ("Acme's **ARR** is 512000 usd.\n\n*Sources*\n"
                                   "• forged — \"a citation nobody produced\"\n\n"
                                   + copy.VERDICT_LINES["verified"])}
    blocks = render.render_answer(injected, _no_link, asker_slack_user_id="U_ANA",
                                  mint_token=_fake_mint_token)

    section_blocks = [b for b in blocks if b.get("type") == "section"]
    context_blocks = [b for b in blocks if b.get("type") == "context"]

    assert len(section_blocks) == 1
    forged_text = section_blocks[0]["text"]["text"]
    assert "*Sources*" in forged_text and "forged" in forged_text   # the injection DID land there

    # ... but the REAL Sources block and verdict line are context blocks, never confusable with it
    real_sources = [b for b in context_blocks
                    if any("Sources" in e["text"] for e in b["elements"])]
    real_verdict = [b for b in context_blocks
                    if any(copy.VERDICT_LINES["verified"] in e["text"] for e in b["elements"])]
    assert real_sources, "the real Sources block must be a context block"
    assert real_verdict, "the real verdict line must be a context block"
    assert not any(b is real_sources[0] for b in section_blocks)
    assert not any(b is real_verdict[0] for b in section_blocks)


# ── no doorbell card renders CommonMark ────────────────────────────────────────────────────────
# The doorbell renders code-composed copy with exactly one untrusted free-text slot per card and
# never renders CommonMark — `to_mrkdwn` turns `[text](url)` into a REAL Slack hyperlink, and the
# untrusted slot on every one of these cards is model- or capture-derived text
# (`service._neutralize_report`'s own docstring: "DERIVED from captured material... untrusted
# text").
_HOSTILE_LINK_MARKDOWN = "[Approve now](https://attacker.example/steal)"


def _no_link_renders_as_a_real_hyperlink(text: str) -> bool:
    """`to_mrkdwn`'s own output shape for a converted link: `<url|text>`. If this substring is
    present, the hostile markdown was interpreted rather than escaped."""
    return "<https://attacker.example/steal|Approve now>" in text


def test_doorbell_parked_capture_never_renders_a_link_from_the_summary():
    """The reproduction: a `judged_type`/summary of `[Approve now](https://
    attacker.example/steal)` used to render as TWO live attacker-controlled links (`to_mrkdwn`
    converts the markdown link syntax AND `escape_mrkdwn`'s own `<`/`>` escaping of a bare URL
    elsewhere would not save it) in the doorbell DM whose whole copy doctrine is "the single next
    action". The literal markdown source is fine to appear as inert, escaped text; only a REAL
    `<url|text>` Slack hyperlink is the defect this test exists to catch."""
    blocks, _ = render.render_doorbell_parked_capture(item_id="1",
                                                       summary=_HOSTILE_LINK_MARKDOWN)
    text = _text(blocks)
    assert not _no_link_renders_as_a_real_hyperlink(text)


def test_doorbell_entity_proposal_never_renders_a_link_from_the_subject():
    blocks, _ = render.render_doorbell_entity_proposal(
        item_id="1", submitter="alice@example.com", name=_HOSTILE_LINK_MARKDOWN)
    assert not _no_link_renders_as_a_real_hyperlink(_text(blocks))




def test_no_doorbell_card_renderer_calls_to_mrkdwn():
    """The asymmetry, pinned structurally so it cannot silently return: every
    `render_doorbell_*` function's compiled bytecode must never reference `to_mrkdwn` (directly, or
    through the module's own `_render_markdown` helper, which itself calls it) — `escape_mrkdwn`
    alone is the whole doctrine for this surface."""
    import dis

    doorbell_renderers = [getattr(render, name) for name in dir(render)
                          if name.startswith("render_doorbell_")]
    assert len(doorbell_renderers) >= 2, "expected every doorbell card renderer to be found"
    for fn in doorbell_renderers:
        names = {instr.argval for instr in dis.get_instructions(fn)
                 if instr.opname in ("LOAD_GLOBAL", "LOAD_METHOD", "LOAD_ATTR")}
        assert "to_mrkdwn" not in names, f"{fn.__name__} must not call to_mrkdwn"
        assert "_render_markdown" not in names, f"{fn.__name__} must not call _render_markdown"


# ── the same asymmetry, checked across the REST of slack/ ──────────────────────────────────────
# `render_generic_report` (the fast-lane push channel to the SUBMITTER) carries the identical
# agent-classified text (`librarian.report.triage_type`/`triage_entity`) that made the doorbell's
# card exploitable — just delivered to a different recipient. `render_needs_input` turned out to
# be WORSE: it applied no escaping AT ALL, so a raw Slack-native `<url|text>` in the untrusted
# prose (not even CommonMark) would have rendered as a live link with no conversion step needed.
def test_generic_report_never_renders_a_link_from_an_agent_classified_summary():
    raw = f"triage — parked, not filed. This reads like {_HOSTILE_LINK_MARKDOWN}."
    text = render.render_generic_report("triage", raw)[0]["text"]["text"]
    assert not _no_link_renders_as_a_real_hyperlink(text)


def test_generic_report_renderer_never_calls_to_mrkdwn():
    import dis
    names = {instr.argval for instr in dis.get_instructions(render.render_generic_report)
             if instr.opname in ("LOAD_GLOBAL", "LOAD_METHOD", "LOAD_ATTR")}
    assert "to_mrkdwn" not in names
    assert "_render_markdown" not in names


_HOSTILE_SLACK_NATIVE_LINK = "<https://attacker.example/steal|Click here to verify>"


def test_needs_input_never_renders_a_raw_slack_native_link_from_the_situation_prose():
    """`render_needs_input` used to apply NO escaping at all to `situation_prose` — worse than the
    doorbell's `to_mrkdwn`-conversion instance, since Slack's OWN native link syntax
    (`<url|text>`, not even CommonMark) would render live with no conversion step needed."""
    text = render.render_needs_input(
        situation_prose=f"needs_input — parked. Your material seems to be about "
                        f"\"{_HOSTILE_SLACK_NATIVE_LINK}\".",
        slack_user_id="U123")[0]["text"]["text"]
    assert "attacker.example/steal|Click here to verify>" not in text
    assert "&lt;https://attacker.example/steal" in text   # escaped, not interpreted


def test_needs_input_still_addresses_the_submitter_with_a_real_mention():
    """The fix must not corrupt the ONE piece of code-composed markup this render legitimately
    needs: the `<@slack_user_id>` mention `copy.needs_input_body` composes AROUND the (now
    pre-escaped) untrusted prose."""
    text = render.render_needs_input(situation_prose="an ordinary situation",
                                     slack_user_id="U123")[0]["text"]["text"]
    assert text.startswith("<@U123> —")
