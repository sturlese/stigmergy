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


def _render_answer(answer: dict, link_resolver, **kwargs) -> list[dict]:
    kwargs.setdefault("asker_slack_user_id", "U_TEST")
    kwargs.setdefault("mint_token", _fake_mint_token)
    return render.render_answer(answer, link_resolver, **kwargs)


# ── the verdict — never flattened ──────────────────────────────────────────────────────────────
def test_verified_answer_renders_the_verified_trust_line():
    blocks = _render_answer(VERIFIED_ANSWER, _no_link)
    assert copy.VERDICT_LINES["verified"] in _text(blocks)
    assert copy.VERDICT_LINES["partial"] not in _text(blocks)


def test_partial_answer_renders_the_partial_trust_line_never_the_verified_one():
    blocks = _render_answer(PARTIAL_ANSWER, _no_link)
    assert copy.VERDICT_LINES["partial"] in _text(blocks)
    assert copy.VERDICT_LINES["verified"] not in _text(blocks)


def test_an_unrecognized_verdict_raises_rather_than_silently_flattening():
    """A missing branch must throw. `failed` never reaches this function in
    production (AnswerService always suppresses it into a refusal first) — this proves the
    renderer does not paper over that invariant if it were ever violated."""
    bogus = {**VERIFIED_ANSWER, "verdict": {"verdict": "failed", "unverified_figures": [],
                                            "citation_problems": []}}
    with pytest.raises(KeyError):
        _render_answer(bogus, _no_link)


# ── confidence is never rendered (developer ruling 6) ────────────────────────────────────────────
def test_confidence_field_never_appears_in_the_rendered_blocks():
    blocks = _render_answer(VERIFIED_ANSWER, _no_link)
    rendered = json.dumps(blocks)
    assert "confidence" not in rendered
    assert VERIFIED_ANSWER["confidence"] not in rendered


# ── the refusal — an answer, not an error ────────────────────────────────────────────────────────
def test_refusal_renders_the_bold_lead_and_the_reason_verbatim():
    blocks = _render_answer(REFUSED_ANSWER, _no_link)
    text = _text(blocks)
    assert text.startswith("*I don't have that.*")
    assert REFUSED_ANSWER["reason"] in text


def test_refusal_never_uses_the_word_refused():
    blocks = _render_answer(REFUSED_ANSWER, _no_link)
    assert "refused" not in _text(blocks).lower()


# ── citations: the no-link contract — there is no read site, so the ONLY resolver production
# wires is `slack.settings.no_link_resolver` ───────────────────────────────────────────────────
def test_a_citation_gets_the_affordance_and_no_link_under_the_no_link_resolver():
    blocks = _render_answer(VERIFIED_ANSWER, _no_link, asker_slack_user_id="U_ANA",
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
    blocks = _render_answer(VERIFIED_ANSWER, _all_link, asker_slack_user_id="U_ANA",
                                  mint_token=_fake_mint_token)
    text = _text(blocks)
    assert "<https://read.example.com/wiki/notes/acme.md|" in text
    assert not any(b.get("type") == "actions" for b in blocks)


def test_no_citations_produces_no_sources_block():
    answer = {**VERIFIED_ANSWER, "citations": []}
    blocks = _render_answer(answer, _no_link)
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
    blocks = _render_answer(DUPLICATE_CITATION_ANSWER, _no_link, asker_slack_user_id="U_ANA",
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
    blocks = _render_answer(TWO_DISTINCT_CITATIONS_ANSWER, _no_link,
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
                                            answer=VERIFIED_ANSWER, link_resolver=_no_link,
                                            asker_slack_user_id="U_TEST",
                                            mint_token=_fake_mint_token)
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


def test_generic_report_bolds_the_enum_prefix_and_keeps_the_rest_of_the_sentence():
    raw = "failed — the librarian could not finish this. Your material is fine."
    text = render.render_generic_report("failed", raw)[0]["text"]["text"]
    assert text.startswith("*failed* — the librarian could not finish this.")
    assert "failed — failed" not in text   # never a duplicated prefix


# ── the Sources block and the verdict line are the bot's OWN structure, and a prompt-injected
# answer body must not be able to imitate them ─────────────────────────────────────────────────
def test_the_real_sources_block_and_verdict_line_render_as_context_blocks_not_sections():
    """`escape_mrkdwn` only escapes `&`/`<`/`>` — asterisks, headers and newlines survive in
    `answer_markdown`. The structural defense is that the REAL Sources block and verdict line
    render as `context` blocks (smaller, grey — Slack's own chrome), a channel the answer BODY
    (always a `section` block) cannot reach into, no matter what it contains."""
    blocks = _render_answer(VERIFIED_ANSWER, _no_link, asker_slack_user_id="U_ANA",
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
    blocks = _render_answer(injected, _no_link, asker_slack_user_id="U_ANA",
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
