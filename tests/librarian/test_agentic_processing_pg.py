"""The ITERATING ordinary flow, end to end (ADR 034): a real Postgres queue, a real git repo + bare
remote, the eight real gates, the real contract linter, and a real `pydantic_ai.Agent` holding the
five real tools — driven by a scripted `FunctionModel` over the backend's own offline seam.

**This is the road ADR 034 re-opened and the road nothing else covers end to end.**
`test_processing_pg.py` drives the same branch with the offline double (no model, no tools, no
outcome-file parse), and `test_structured_processing_pg.py` drives the branch where CODE writes the
page. What only lives here is the pairing the milestone actually ships: a model that WRITES its page
through a confined tool and returns its account as `.librarian-outcome.json`, with the worker
reading that file, cross-checking it against the diff, and committing.

**Four of the cases below are the coverage the developer re-opened by hand**, and each one is a road
the file channel has and the envelope channel did not:

  * a model that writes NO account — the worker paid for a run and has nothing to file;
  * one that writes an UNPARSEABLE account — the same road, one refusal over;
  * one whose account is SHAPE-refused — which must reach the corrective retry carrying the
    findings, and file when the second answer is repaired;
  * one that PARKS and leaves a page behind anyway — "supposed to have written nothing" is not a
    check, and the diff decides here as everywhere else.

**And the SEED, which is the other half of D4**: an exploring backend that declares
`wants_gathered = True` is gathered for, is told in the block itself that the block is a starting
point rather than a boundary, and is gathered for AGAIN on the corrective pass over a worktree that
was put back. The offline double declares `False` and is handed nothing — the same branch in both
of its states, which is what stops "it explores" from meaning "it starts from nothing".

**Nothing here is faked below the port.** The queue, the evidence store, git, the worktree, the
gates, the linter and the tools are production's; the injected seams are the backend's own
`model_factory` (a `FunctionModel`, never a provider) and — where a test has to observe what
`processing` HANDED the backend — a recording wrapper that forwards both port declarations, which
is the shape every rig in this repo wraps a backend with.
"""
import dataclasses
import json
import pathlib

from stigmergy.capture import schema
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import gitcmd, processing, worker
from stigmergy.librarian.double import DoubleAgent
from stigmergy.librarian.pydantic_backend import PydanticFilingAgent
from tests.librarian import support

PRICED_MODEL = "openai:gpt-5.6-terra"


def _scaffold_of(payload: str) -> dict:
    """The plain-JSON SCAFFOLD half of a tool result the model received.

    Tool results now frame their page-derived content like `agent.render_gathered` — a plain-JSON
    scaffold, then `agent.fence(json.dumps(content))` (H1) — so the whole string is not one
    `json.loads`-able value. The identifiers a test asserts on (`search_pages`' `matches`) live in
    the scaffold; this returns it, tolerating the plain-JSON results that carry no fenced half."""
    opened, _closed = agent_module.fence("\x00M\x00").split("\x00M\x00")
    return json.loads(payload.partition(opened)[0]) if opened in payload else json.loads(payload)

REGISTERED = "Acme Corp"
MATERIAL = ("The Acme Corp renewal window was confirmed at the sync, with the pilot scope "
            "unchanged.")

TITLE = "Acme Corp Renewal Window"
PAGE_PATH = f"wiki/notes/{TITLE}.md"

# Digit-free padding, for the offline double's own reason: any numeral in a drafted body reads as a
# figure the capture asserted, and that is exactly what `gates` judges.
_FILLER = [
    "This page records what the capture carried, in the brain's own vocabulary.",
    "It is structured for retrieval rather than for reading end to end.",
    "Nothing here asserts anything the captured material did not carry.",
]


def _page_text(*, title: str = TITLE, anchor: str = REGISTERED) -> str:
    """One whole page as the MODEL writes it — frontmatter included, because on this road the agent
    is the author and code only stamps what the server owns.

    Deliberately the same shape the offline double writes (`double._write_page`): it is the shape
    the eight gates and the frozen contract linter are known to accept, so a failure in a test below
    is about the road being exercised rather than about this fixture's prose.
    """
    front = ["type: note", f'title: "{title}"', "status: developing",
             f"created: {support.FIXED_TODAY}", f"updated: {support.FIXED_TODAY}",
             "tags: [note]", f'related: ["[[{anchor}]]"]', "sources: []"]
    body = [f"# {title}", ""]
    body += _FILLER
    body += ["", f"This material is about [[{anchor}]].", "", "## What the capture said", ""]
    body += [line for line in MATERIAL.splitlines()]
    body += ["", "## Why it is here", ""]
    body += _FILLER
    body += ["", "## Connections", "", f"- [[{anchor}]] — the entity this material belongs to"]
    while len([line for line in body if line.strip()]) < 32:
        body.append("Additional context recorded from the capture for future readers.")
    return "---\n" + "\n".join(front) + "\n---\n\n" + "\n".join(body) + "\n"


def _account(**overrides) -> dict:
    """The agent's own account of the page it just wrote, in the LEGACY envelope's shape: it names
    the path it wrote and carries no page text at all."""
    account = {
        "decision": "file",
        "page_path": PAGE_PATH,
        "page_type": "note",
        "title": TITLE,
        "anchoring": {"kind": "entity", "entities": [REGISTERED], "reason": ""},
        "links_created": [REGISTERED],
        "overlaps": [], "edits": [], "findings": [],
        "summary": "filed the renewal note",
    }
    account.update(overrides)
    return account


class Script:
    """One scripted run of the ordinary flow, across as many WORKER passes as the item takes.

    `steps` is a list of `(tool, arguments)` per pass — the model's whole conversation — and the
    last request of every pass returns a final message the backend ignores by design. The script
    records the PROMPT it was handed on each pass, which is how the corrective-retry case below
    asserts that the second pass was told what was wrong rather than merely asked again.

    A `model_factory` is called once per pass by the backend, so the pass counter lives there.
    """

    def __init__(self, *passes):
        self.passes = list(passes)
        self.index = -1
        self.prompts = []
        self.tool_results = []

    def factory(self):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
        from pydantic_ai.models.function import FunctionModel

        self.index += 1
        steps = self.passes[min(self.index, len(self.passes) - 1)]

        def _script(messages, info):
            turn = len([m for m in messages if m.kind == "request"])
            if turn == 1:
                self.prompts.append("\n".join(
                    getattr(part, "content", "") for message in messages
                    for part in getattr(message, "parts", ())
                    if part.part_kind in ("user-prompt", "system-prompt")))
            self.tool_results = [(part.tool_name, part.content)
                                 for message in messages
                                 for part in getattr(message, "parts", ())
                                 if isinstance(part, ToolReturnPart)]
            if turn <= len(steps):
                tool, arguments = steps[turn - 1]
                return ModelResponse(parts=[ToolCallPart(tool, arguments)])
            return ModelResponse(parts=[TextPart("that is my account")])

        return FunctionModel(_script)


def _write_page_step(text: str | None = None, path: str = PAGE_PATH):
    return ("write_page", {"path": path, "content": _page_text() if text is None else text})


def _write_account_step(account: dict | str | None = None):
    raw = account if isinstance(account, str) else json.dumps(_account() if account is None
                                                              else account)
    return ("write_page", {"path": agent_module.OUTCOME_FILENAME, "content": raw})


_SEARCH_STEP = ("search_pages", {"query": "renewal window"})


def _rig(tmp_path, script: Script, *, wrapper=None):
    """A `RepoEnv` + `Deps` whose agent is the REAL backend over `script`'s offline model.

    `backend="pydantic"` on the settings as well as in `Deps.agent`: the row this flow writes
    records the configured backend, and a rig that disagreed with itself would stop measuring a
    configuration a worker can hold.
    """
    env = support.build_repo(str(tmp_path / "git"))
    settings = support.build_settings(env, worktree_root=str(tmp_path / "worktrees"),
                                      backend="pydantic", model=PRICED_MODEL)
    agent = PydanticFilingAgent(settings, model_factory=script.factory)
    if wrapper is not None:
        agent = wrapper(agent)
    return env, support.build_deps(env, settings, agent=agent)


def _file(conn, deps, material: str = MATERIAL, **kwargs):
    support.submit(conn, deps, material, **kwargs)
    return worker.process_next(conn, deps)


def _nothing_landed(env, before: set) -> None:
    """No commit reached the bare remote on ANY ref — a walk of the object database rather than a
    diff of two return values, because a local commit that was never pushed is still evidence a
    write happened."""
    assert support.all_commit_shas(env.bare) == before, (
        "the bare remote gained a commit for a capture that was supposed to be refused")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# the road itself: a model that looks, writes its page, and accounts for it
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_an_iterating_run_files_the_page_the_MODEL_wrote_and_the_row_says_what_it_cost(
        tmp_path, clean_queue, require_gitleaks):
    """**The benign twin the whole file needs, and the shape ADR 034 ships.**

    Four requests: one search, the page, the account, a final message nobody reads. Everything after
    that is production's — `read_outcome` parses the file, `discard_outcome_file` drains the channel
    before the diff is taken, `_stamp` writes the server-owned frontmatter, the eight gates and the
    contract linter judge the diff, and the commit is pushed.

    The three assertions that could each go wrong silently: the committed page is the one the MODEL
    wrote (not a code-written one — this backend declares `structured_ordinary = False`), the
    account file is not in the commit, and the row carries a real, positive spend.
    """
    script = Script([_SEARCH_STEP, _write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    committed = support.changed_paths(env.repo, sha)
    assert committed == [PAGE_PATH], committed
    assert agent_module.OUTCOME_FILENAME not in support.all_ever_committed_paths(env.bare), (
        "the agent's own account reached a commit — the channel was not drained before the diff")
    filed_page = support.read_filed_page(env.repo, sha, PAGE_PATH)
    assert "This material is about [[Acme Corp]]." in filed_page, (
        "the committed page is not the one the model wrote")
    assert result.report["cost_usd"] > 0, "a real framework run was banked as free"


def test_the_report_that_reaches_a_submitter_carries_no_loop_counters(tmp_path, clean_queue,
                                                                       require_gitleaks):
    """`turns` and `tool_calls` carry real numbers again (D6) and NOTHING downstream reads them:
    `report.filed` has no field for either, so a backend that started counting changes no surface a
    person or a caller sees — and the row still serializes into `capture_queue.report`'s `jsonb`
    column, which is where a field nobody expected would actually bite.

    Asserted over every KEY the persisted report carries, at any depth, rather than over a
    hand-picked one; keys and not the flattened JSON, because the report carries prose too and a
    summary containing "returns" is not a field called `turns`.
    """
    script = Script([_SEARCH_STEP, _write_page_step(), _write_account_step()])
    _, deps = _rig(tmp_path, script)

    item, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    counters = [key for key in _every_key(result.report)
                if any(word in key.lower() for word in ("turn", "tool_call", "request"))]
    assert not counters, f"the filed report grew a loop counter: {counters}"
    # ...and the row really did land in the queue with that report, which is the surface a caller
    # reads it from — a report that only existed in memory would prove nothing about the column.
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (item["id"],))
        assert cur.fetchone()[0] == schema.FILED


def _every_key(node) -> list:
    """Every mapping key in a nested structure, at any depth."""
    if isinstance(node, dict):
        return [str(key) for key in node] + [k for value in node.values()
                                             for k in _every_key(value)]
    if isinstance(node, (list, tuple)):
        return [k for value in node for k in _every_key(value)]
    return []


# ══════════════════════════════════════════════════════════════════════════════════════════════
# the outcome FILE's own failure roads — re-opened coverage (the envelope channel had none)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_model_that_never_writes_its_account_lands_terminal_with_nothing_committed(
        tmp_path, clean_queue, require_gitleaks):
    """**A run that wrote a page and said nothing has filed nothing.**

    The model's final message says it filed — and is ignored by design, because reading the prose
    would invent an account. `read_outcome` refuses, the fault travels as a bare `AgentError`, and
    `worker.process_next` names the stage from the exception's own class rather than shrugging
    "unexpected". The page the model drafted dies with the worktree.

    Priced, because the requests were real: a `failed` row that reports `$0.00` after paying for a
    full loop is the exact instrument failure this backend exists to close.
    """
    script = Script([_SEARCH_STEP, _write_page_step()])          # no account, ever
    env, deps = _rig(tmp_path, script)
    before = support.all_commit_shas(env.bare)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FAILED, result.report.get("summary")
    assert result.report["stage"] == "AgentError", result.report
    assert result.report["stage"] != "unexpected", (
        "a known refusal surfaced as an unnamed crash with a traceback")
    assert agent_module.OUTCOME_FILENAME in result.report["summary"], (
        f"the failure does not name the channel that was empty: {result.report['summary']}")
    assert result.report["cost_usd"] > 0, "the requests were real and the row says free"
    _nothing_landed(env, before)


def test_an_unparseable_account_takes_the_same_road_and_names_the_parse(
        tmp_path, clean_queue, require_gitleaks):
    """One refusal over: the file is there and is not JSON. Same terminal road, and the message
    carries the CLASS of the parse failure rather than the bytes — an account written by a model
    that has just read untrusted material is untrusted input, and echoing it into an operator's log
    is how captured material reaches a surface nobody sanitized."""
    script = Script([_write_page_step(), _write_account_step("{not json at all")])
    env, deps = _rig(tmp_path, script)
    before = support.all_commit_shas(env.bare)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FAILED, result.report.get("summary")
    assert result.report["stage"] == "AgentError"
    assert "JSONDecodeError" in result.report["summary"], result.report["summary"]
    assert "not json at all" not in result.report["summary"], (
        "the refusal echoed the account's own bytes")
    _nothing_landed(env, before)


def test_an_account_the_boundary_refuses_by_SHAPE_buys_a_corrective_pass_that_is_told_why(
        tmp_path, clean_queue, require_gitleaks):
    """**The shape road, which is the one class of failure saying so can fix** — and the twin that
    proves the retry is worth spending.

    First pass: an account with no `title`. `parse_outcome` refuses it as an `OutcomeShapeError`
    carrying a `gates.Finding`, which `_run_in_worktree` turns into a corrective brief for the
    SECOND pass rather than a terminal row. Second pass: the same model, repaired — and the capture
    files.

    Two assertions carry the "told why" half: the second prompt exists (the retry happened) and it
    names the missing field (the brief travelled). A retry that merely re-asked would pass a
    pass-count assertion and teach the model nothing.

    **The evidence is the PROMPTS and not the report**, deliberately: `report.filed` carries no
    agent-attempt counter at all — that field belongs to `report.failed_system`, where a person
    reading a `failed` row needs to know how many passes were paid for. A filed capture is filed
    whether it took one pass or two.
    """
    script = Script(
        [_write_page_step(), _write_account_step(_account(title="", page_type="note"))],
        [_write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    assert len(script.prompts) == 2, (
        f"the shape refusal did not buy a corrective pass: {len(script.prompts)} pass(es)")
    assert "title" in script.prompts[1], (
        "the corrective pass was re-asked without being told what was wrong")
    assert script.prompts[1] != script.prompts[0], "the second pass got the same prompt as the first"
    assert result.report["cost_usd"] > 0, "two paid passes were banked as free"
    _, sha = result.result_ref.rsplit("@", 1)
    assert support.changed_paths(env.repo, sha) == [PAGE_PATH]


def test_a_model_that_stays_shape_refused_lands_terminal_after_exactly_two_passes(
        tmp_path, clean_queue, require_gitleaks):
    """The other end of the same tolerance: an account that is never repaired spends the one
    corrective pass and stops. Terminal, priced, nothing committed — and NOT a third pass, because
    the retry budget is one and a loop over a model that cannot answer is a bill with no end."""
    script = Script([_write_page_step(), _write_account_step(_account(title=""))])
    env, deps = _rig(tmp_path, script)
    before = support.all_commit_shas(env.bare)

    _, result = _file(clean_queue, deps)

    assert result.status in (schema.FAILED, schema.TRIAGE), result.report.get("summary")
    assert len(script.prompts) == 2, f"{len(script.prompts)} agent pass(es), expected exactly 2"
    assert result.report["cost_usd"] > 0
    _nothing_landed(env, before)


def test_an_account_that_parks_while_leaving_a_page_behind_is_refused(
        tmp_path, clean_queue, require_gitleaks):
    """**"It is SUPPOSED to have written nothing" is not a check, and this is the check.**

    A `triage` outcome with a diff behind it is an agent that wrote and then said it did not — so
    the worktree decides, as it does everywhere else in this flow. The refusal counts the changes it
    found, which is what tells an operator this was a stray write rather than an empty park.

    Reachable only on THIS road: a structured backend has no write tool at all, so the case exists
    for a backend that holds one — which is both shipped ordinary backends since ADR 034.
    """
    script = Script([_write_page_step(),
                     _write_account_step({"decision": "triage",
                                          "triage": {"kind": "unresolved-entity",
                                                     "name": "Halcyon Grid"},
                                          "summary": "cannot place this"})])
    env, deps = _rig(tmp_path, script)
    before = support.all_commit_shas(env.bare)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FAILED, result.report.get("summary")
    assert "parked" in result.report["summary"] and "change(s)" in result.report["summary"], (
        result.report["summary"])
    _nothing_landed(env, before)


def test_a_run_that_writes_TWO_pages_is_refused_by_the_cross_check(
        tmp_path, clean_queue, require_gitleaks):
    """**A hostile shape ADR 034 re-opened, and the tool cannot refuse it.**

    `agent.confined_write` bounds each write to ONE new `.md` page in a lane folder — it says
    nothing about how many such writes a run may make, and a `write_page` tool that counted them
    would be a second answer to a question `_cross_check_outcome` already answers from the diff.
    So on this road a model really can create two pages, and the only thing between that and a
    commit is the cross-check's "exactly one".

    It matters because of what `_file` does with the diff: it takes the alphabetically first new
    page for `page_path`, `result_ref`, the commit subject, the dedup pointer and the whole report
    — so a second page would be committed, stamped and pushed while appearing on no surface a human
    reads, and `gate_anchoring` unions wikilinks across all new pages, letting an unanchored page
    ride in on the first one's coat-tails.

    The structured road cannot stage this at all (code writes the page), which is exactly why it
    belongs here.
    """
    second = "wiki/notes/A Second Page.md"
    script = Script([_write_page_step(),
                     _write_page_step(_page_text(title="A Second Page"), path=second),
                     _write_account_step()])
    env, deps = _rig(tmp_path, script)
    before = support.all_commit_shas(env.bare)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FAILED, (
        f"two pages were filed under one capture: {result.report.get('summary')}")
    assert result.report["stage"] == "outcome", result.report
    assert "created 2 pages in one capture" in result.report["summary"], result.report["summary"]
    # ...and it cost the corrective retry, which is the honest outcome rather than a defect: the
    # finding names a repair the agent could perform (write one page), so the second pass is spent
    # asking for it. A model that writes two pages twice is refused, not looped over.
    assert result.report["agent_attempts"] == 2, result.report
    _nothing_landed(env, before)


def test_a_park_that_really_wrote_nothing_is_parked_rather_than_refused(
        tmp_path, clean_queue, require_gitleaks):
    """The stray-write check's benign twin, and it is the case an operator meets constantly: a
    capture about something the registry does not know is PARKED on a human, not failed. A check
    that could only ever refuse would turn every honest park into a system fault."""
    script = Script([_SEARCH_STEP,
                     _write_account_step({"decision": "triage",
                                          "triage": {"kind": "unresolved-entity",
                                                     "name": "Halcyon Grid"},
                                          "summary": "the registry does not know this"})])
    env, deps = _rig(tmp_path, script)
    before = support.all_commit_shas(env.bare)

    _, result = _file(clean_queue, deps)

    assert result.status in schema.PARKED_STATUSES, result.report.get("summary")
    assert "Halcyon Grid" in json.dumps(result.report)
    _nothing_landed(env, before)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# D4 — the SEED: `wants_gathered` is a second declaration, and the block says what it is for
# ══════════════════════════════════════════════════════════════════════════════════════════════
class Recording:
    """A wrapper that records what `processing` handed the backend, and forwards BOTH declarations.

    The shape every rig in this repo wraps a backend with (`evals.run_filing.CountingAgent`,
    `support.DelayedAgent`), and copied by plain attribute access with no default on purpose: a
    wrapper that swallowed either member is the failure `filing_port` refuses loudly.
    """

    def __init__(self, inner):
        self.inner = inner
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.gathered_seen = []
        self.correctives = []

    def run(self, **kwargs):
        self.gathered_seen.append(kwargs.get("gathered"))
        self.correctives.append(kwargs.get("corrective"))
        return self.inner.run(**kwargs)

    def run_meeting(self, **kwargs):                     # pragma: no cover — never called here
        return self.inner.run_meeting(**kwargs)


def test_the_exploring_backend_is_seeded_and_told_the_seed_is_a_starting_point(
        tmp_path, clean_queue, require_gitleaks):
    """**The whole of D4 at the seam that decides it.**

    `wants_gathered = True` means the gatherer runs for a backend that ALSO writes its own page —
    the two questions came apart, and a flow that derived one from the other would hand the shipped
    backend an empty seed while it held five tools.

    The wording is the second half and it is not cosmetic. A run told "this is your context and you
    have no tool to go looking for more" while holding `search_pages` does not error: it quietly
    declines to use them, and the measurement that decides whether iteration is worth its cost comes
    back saying it is not. So the SEEDED preface must be there and the tool-less default must not.

    Asserted twice over, at the port and in the prompt the MODEL was handed: `processing` composing
    the right block would still be a defect if `build_prompt` dropped it on the way.
    """
    script = Script([_SEARCH_STEP, _write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script, wrapper=Recording)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    gathered = deps.agent.gathered_seen[0]
    assert gathered, "an exploring backend that declares `wants_gathered` was handed nothing"
    assert processing._SEEDED_GATHERED_SENTENCES["preface"] in gathered
    assert agent_module.GATHERED_PREFACE_NO_TOOLS not in gathered, (
        "the seeded run was told it has no tool to go looking with")
    assert "Acme Corp" in gathered, "the gather ran but found nothing this brain holds"
    assert processing._SEEDED_GATHERED_SENTENCES["preface"] in script.prompts[0], (
        "the block was built and never reached the model's own prompt")


def test_the_offline_double_declares_it_wants_none_and_is_handed_none(tmp_path, clean_queue,
                                                                       require_gitleaks):
    """The specificity half, and the reason `wants_gathered` is read at all rather than assumed of
    every exploring backend: the double is directive-driven, so a gather for it is a
    `corpus.load_pages` walk per pass whose rendered string nothing reads.

    The suite runs on this backend, so the cost would be paid by every processing test in the
    package.
    """
    env, deps = support.build_rig(tmp_path)
    recording = Recording(DoubleAgent(deps.settings))
    deps = dataclasses.replace(deps, agent=recording)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    assert recording.gathered_seen == [""], (
        f"a backend declaring `wants_gathered = False` was gathered for: "
        f"{recording.gathered_seen!r}")


def test_the_corrective_pass_is_seeded_again_over_a_worktree_that_was_put_back(
        tmp_path, clean_queue, require_gitleaks):
    """The seed is REBUILT per pass, not carried over.

    `_reset_for_retry` puts the worktree back before the second pass, so a context computed once and
    reused would describe a checkout that no longer exists — and the second pass would judge overlap
    against pages the first pass's own draft had changed. Both passes are gathered for, and both
    blocks describe the same base commit.
    """
    script = Script(
        [_write_page_step(), _write_account_step(_account(title=""))],
        [_write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script, wrapper=Recording)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    seen = deps.agent.gathered_seen
    assert len(seen) == 2 and all(seen), f"the corrective pass was not gathered for: {seen!r}"
    assert seen[0] == seen[1], (
        "the two passes were handed different contexts — the worktree was not put back before the "
        "second gather, or the block is not deterministic")
    assert deps.agent.correctives[1], "the second pass carried no corrective brief"


def test_a_wrapper_that_forwards_only_HALF_the_port_fails_loudly_on_the_first_item(
        tmp_path, clean_queue, require_gitleaks):
    """**The refusal `processing._wants_gathered` exists for, exercised where it fires.**

    A wrapper is written per rig by somebody thinking about the one thing they are counting, which
    is how the port's newest member went missing in ten places at once. A `getattr(..., False)`
    default would have run the shipped backend with an EMPTY seed — silently, on a road whose whole
    output is a filing-quality score — so absence is refused instead, and the refusal names the
    member, the port and the fix.

    `test_filing_port_conformance.py` covers the wrappers that SHIP; this covers what the flow does
    when one of them is written wrong, which is the half a conformance test cannot reach.
    """
    class HalfForwarding:
        def __init__(self, inner):
            self.inner = inner
            self.structured_ordinary = inner.structured_ordinary

        def run(self, **kwargs):                        # pragma: no cover — never reached
            return self.inner.run(**kwargs)

        def run_meeting(self, **kwargs):                # pragma: no cover — never reached
            return self.inner.run_meeting(**kwargs)

    script = Script([_write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script, wrapper=HalfForwarding)
    before = support.all_commit_shas(env.bare)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FAILED, result.report.get("summary")
    assert result.report["stage"] == "AgentError", result.report
    summary = result.report["summary"]
    assert "wants_gathered" in summary and "filing_port" in summary, summary
    assert script.prompts == [], "the backend was called at all — the refusal fires before the run"
    _nothing_landed(env, before)


def test_a_wrapper_that_forwards_both_members_files_exactly_as_the_backend_would(
        tmp_path, clean_queue, require_gitleaks):
    """The refusal's benign twin, and the one that keeps it from being a tax on every rig: a wrapper
    that copies both declarations is invisible to the flow. `Recording` above IS such a wrapper and
    every seeded case in this file rides on it — this states the property those tests depend on
    instead of leaving it implied."""
    script = Script([_write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script, wrapper=Recording)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    assert (deps.agent.structured_ordinary, deps.agent.wants_gathered) == (False, True)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# the tools reach THIS item's checkout, which is what makes the seed and the search agree
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_the_search_tool_reads_the_items_own_worktree_and_not_the_operators_checkout(
        tmp_path, clean_queue, require_gitleaks):
    """The tools are built over the worktree `processing` created for this item — the checkout at
    its BASE COMMIT — so what a model finds by searching is the same graph the gather described.

    Proven by a page that exists only in the base commit and by the ephemeral worktree's own path:
    the tool result names the fixture's page, and the run happened somewhere under
    `settings.worktree_root` rather than in the operator's `STIGMERGY_REPO`.
    """
    script = Script([_SEARCH_STEP, _write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    name, payload = script.tool_results[0]
    assert name == "search_pages"
    matches = _scaffold_of(payload)["matches"]
    assert any(m["path"] == "wiki/notes/Café Zürich Renewal.md" for m in matches), (
        f"the search tool did not see the base commit's own pages: {payload[:300]}")
    assert not pathlib.Path(env.repo, PAGE_PATH).exists(), (
        "the agent wrote into the operator's checkout instead of the item's worktree")


def test_the_worktree_the_tools_wrote_in_is_gone_afterwards(tmp_path, clean_queue,
                                                             require_gitleaks):
    """Every write this run made lived in an ephemeral worktree, and `process_item` removes it
    whatever the outcome. Asserted through `git worktree list` rather than by looking for a
    directory, because a worktree git still knows about is the leak that matters — it holds a lock
    and a lease the next run has to reap."""
    script = Script([_write_page_step(), _write_account_step()])
    env, deps = _rig(tmp_path, script)

    _, result = _file(clean_queue, deps)

    assert result.status == schema.FILED, result.report.get("summary")
    listed = gitcmd.run("worktree", "list", cwd=env.repo).stdout
    assert gitcmd.WORKTREE_PREFIX not in listed, listed
