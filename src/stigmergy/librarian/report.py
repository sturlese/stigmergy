"""What a person is told about their capture — one fact set, rendered two ways.

Every terminal state composes its sentence here and nowhere else. `brain_submissions` returns
the structured fact set; `stigmergy-librarian once` prints prose. They are the same object: the
CLI does not compose its own wording, so the two surfaces cannot drift into saying different
things about the same row — a defect class this codebase has shipped twice.

**The rules the wording follows**, all of them load-bearing:

- **State the fact, never the implication.** `filed` names the page and the commit. It must not
  say the brain now knows this: the page becomes searchable only at the next index rebuild or
  the webhook's incremental upsert, and that clause is the SECOND CLAUSE OF THE SAME SENTENCE,
  not a trailing footnote a future edit can drop.
- **Every sentence names the literal enum value** (`filed`, `rejected`, `triage`, `failed`), so
  a person can grep any surface for a state and find it.
- **Never echo the offending value.** A secret in an error message is a secret in a log; an
  injection quoted back is a second copy of the attack delivered to a human. Locators, rule ids
  and categories only.
- **A refusal carries a `reason_code`, and it is not decoration.** The sentence is for a person;
  the code (`capture.schema.REJECTION_REASONS`) is the only thing a READ path may branch on. It
  exists because one had to: `brain_submissions` was serving back the excerpt of the very capture
  a secrets refusal had just bounced — in the same object as the sentence saying it had not — and
  there was nothing structured on the row to tell that class apart. `stage` looked like the
  signal and is written only by `failed_system`; the alternative was matching on this file's
  prose, which would make a confidentiality property change whenever a sentence is improved.
  Every `rejected` builder goes through `_rejected` so none can ship without one.
- **`failed` never shares a sentence shape with `rejected`** — the corrective action is the
  opposite. `rejected` says "fix this and resubmit"; `failed` says the librarian could not comply,
  nothing was filed, and a steward has to look. It must NOT say resubmitting will hit the same
  fault: the agent is not deterministic, so an agent-misbehaviour failure is neither the
  submitter's fault nor a reproducible system fault. That claim shipped on a real walk under a
  zone veto and was simply untrue.
- **A number in a message names which number it is.** `failed` carries TWO counters that a reader
  will otherwise conflate: the queue DELIVERY (`capture_queue.attempts`, the lease) and the AGENT
  attempts inside that delivery (the first pass plus its one corrective retry). "after 1 attempts"
  reported the first while the second was what the operator needed, so nobody could tell whether
  the corrective retry had run.
- **Nothing is silently omitted.** `links_created`, `overlaps_flagged` and `pages_edited` are
  always present; empty renders as `(none)` rather than as a blank — the same "silence is not an
  outcome" principle anchoring follows. `pages_edited` in particular is a page belonging
  to somebody ELSE that this capture changed, so leaving it out was not an omission of detail but
  of the news.
- **The facts say WHAT; `agent_rationale` says WHY.** Everything above is what code observed — page,
  commit, anchor, links, overlaps. None of it says why this type, why this folder, why
  that anchor, and the librarian's whole design rests on the agent judging those. The agent writes
  exactly that account (`agent.Outcome.summary`, which the skill asks for as "one sentence a human
  reads about what you filed and why it went there") and it was collected, bounded and then thrown
  away. It is surfaced under a name that says whose account it is, because it is a CLAIM and not a
  fact: the gates have already refused any disagreement between it and the diff, but the sentence is
  the agent's own and a reader must not mistake it for the system's. It is also what makes a
  wrong anchor correctable — a submitter who sees the anchor AND the reasoning behind it can
  object far better than one who sees only the anchor.

Echoed values (a figure the submitter wrote, a page title, the agent's own prose) go through
`stigmergy.text.sanitize`, the same control-character seam `capture.cli` uses for untrusted text
headed to a terminal — one code path, not two. That is what makes surfacing model prose safe
rather than a second channel: `never echo the offending value` above is about a SECRET or a
planted instruction
quoted as such, and the agent is instructed never to quote one back (it records a category
instead, which arrives here as a `finding`). `anchoring.reason` already crosses this way, through
the same `_clean`; `agent_rationale` is the same class of value on the same seam.
"""
from stigmergy import text as textutil
from stigmergy.capture import schema
from stigmergy.librarian import page as page_policy

NONE_LABEL = "(none)"

# How much of the agent's own account a person is shown. Wider than the 200 characters every other
# prose field here is clamped to, and deliberately: those are a decoration on a fact (the reason
# beside an anchor, the message beside a stage), while this field's CONTENT is the whole point of
# carrying it. 200 would truncate the median real one — the walk that motivated `agent.MAX_PROSE_LEN`
# produced a summary past 400 characters, twice — so the clamp would routinely cut the sentence it
# exists to show. 400 is one long sentence (~60 words): past any plausible reading of the skill's
# "one sentence" and still a bound, with `agent.MAX_PROSE_LEN` (2000) bounding what may reach the
# database at all.
RATIONALE_WIDTH = 400

# The one place the not-yet-searchable clause is written, and the one shape every state's report
# takes. **Both now live in `capture.schema`** and are re-exported here under the names every call
# site already uses: `resolved` and a steward's `rejected` are composed by `capture.dispositions`
# (the steward's tooling), `capture` may not import `librarian`, and a second copy of either would
# be exactly the drift this module exists to prevent — one clause promising a submitter their page
# is not searchable yet, and another, on the sibling state, forgetting to.
#
# Nothing about the rule changes: every sentence a person reads about a FAST-LANE outcome is still
# composed in this file and nowhere else. The vocabulary two packages must agree on moved down to
# the package that owns the column it is stored in; the wording stayed with each writer.
SEARCHABILITY_NOTE = schema.SEARCHABILITY_NOTE
base_report = schema.base_report

# How many registry candidates the ask-back question LISTS before it stops listing and says so.
#
# Deliberately smaller than `gates.MAX_BRIEF_REGISTRY_NAMES` (40), which bounds the same fact for a
# different reader: that one goes into an agent's prompt, this one into a message a non-technical
# person reads at the moment they are being asked to do something. Twenty named things is already a
# long message to read cold.
#
# The rule either side of the cutover is `gates.anchoring_brief`'s, and it is the load-bearing part
# rather than the number: **never a silently truncated candidate list.** Below the cutover the
# question shows the registry in full; above it, it shows NONE and names the count. A ranked subset
# was the drafted alternative and is the more dangerous shape — "not in the list" reads as "not
# registered" to a reader who cannot know the list was filtered, and answering "it's new" to that is
# precisely how `acme-slides.md` gets minted next to `Acme Capital.md`. Asking for the exact name
# instead costs the reader nothing: an alias resolves as well as a canonical name on the next pass.
MAX_QUESTION_CANDIDATES = 20


def _clean(text: str, width: int = 0) -> str:
    """Untrusted text on its way to a human. Same seam as `capture.cli._clean` — and now literally
    the same code: the word-safe truncation this function pioneered lives in `stigmergy.text.clamp`
    beside `stigmergy.text.sanitize`, so the two packages' renderers cannot disagree about it again
    (they did once, and that cost a refusal message that was not a runnable command)."""
    return textutil.clamp(textutil.sanitize(str(text or "")).replace("\n", " "), width)


# An IDENTITY field is not prose, and this is the stricter filter it gets on top of `_clean`.
#
# `schema.SITUATION_NAME_KEY` is written from the agent's reading of CAPTURED MATERIAL and is read
# back by `stigmergy-entities show`, which offers it to a steward as the `--name` of a command to
# paste into a shell holding this operator's push identity for `main` and the queue DSN.
# `sanitize` strips control characters and nothing else — `$`, backticks, quotes and `;` all
# survive it, because it was written for prose headed to a terminal, which this is not.
#
# So the characters that can only be punctuation in a name and can be *syntax* somewhere else are
# dropped here, at the one place this field is written. Two sources, unioned: everything
# `entities.birth._FORBIDDEN_IN_NAME` refuses in a real entity name (a name that cannot be a
# filename or a wikilink is not a name worth suggesting) plus the shell's own metacharacters.
#
# **Stated here rather than imported from `entities.birth`**, deliberately. `stigmergy.entities`
# imports `librarian`, not the reverse (see both packages' `__init__` docstrings): an import in
# this direction would make the unattended worker depend on the steward's CLI, which is backwards
# and circular. The duplication is small, it is one direction of a stricter-is-safe rule, and it is
# called out at both ends — the same posture `entities.generator.FIX_COMMAND` takes about the copy
# of itself in the knowledge repo's own linter. It is also defence in depth rather than the
# defence: `entities.cli` gates the same value through an ALLOW-list before printing it, which is
# strictly stronger and is what protects the rows parked before this filter existed.
_UNSAFE_IN_IDENTITY = set('/\\:*?"<>|[]#^') | set("'`$;&(){}!~\n\r\t")


def _clean_identity(text: str, width: int = 0) -> str:
    """`_clean`, then the shell/filename metacharacters stripped. For NAMES, never for prose.

    Stripped rather than refused: this runs while the librarian is parking a capture it has already
    decided it cannot file, and a capture must always be parkable. Failing here would turn a
    hostile name into a lost row — trading a steward reading a slightly shortened name for a
    submitter's material vanishing.
    """
    return _clean("".join(c for c in str(text or "") if c not in _UNSAFE_IN_IDENTITY), width)


def _plural(count: int, singular: str, plural: str = "") -> str:
    """`1 attempt` / `2 attempts`. A message that says "1 attempts" tells a reader nobody read it."""
    return f"{count} {singular if abs(count) == 1 else (plural or singular + 's')}"


def _listed(values) -> str:
    return ", ".join(_clean(v, 120) for v in _as_list(values)) if values else NONE_LABEL


def _as_list(value) -> list:
    """Anything into a list, without raising.

    Defence in depth behind `agent.parse_outcome`, which is where an outcome's shape is actually
    established. It matters here because these functions run AFTER the commit and the push: a
    `TypeError` at this point left the page on `main`, the row `failed`, and the submitter reading
    "nothing was filed… this is a system fault". A report is the last place that may crash.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _path_of(entry) -> str:
    """The `path` of an overlap entry, whichever shape it arrived in."""
    return str(entry.get("path", "")) if isinstance(entry, dict) else str(entry)


def _anchor_phrase(anchoring: dict, registry=None) -> str:
    r"""`anchored to X` reads the same for both anchoring outcomes, because both ARE anchoring
    outcomes — one names an entity, the other declares scope with a reason.

    The page's own `entity:` frontmatter is stamped with the RESOLVED registry id, so this phrase
    has to carry the same id — spelled the same way — for the report and the page to be legible as
    the same claim: same value, two places, one source. `registry` (`ctx.registry`/`deps.registry`,
    the same object
    `gate_anchoring` verified against) resolves each declared value to its canonical id and display
    name; both are shown together, id backtick-quoted, so a non-technical reader has the name and
    the literal value a `git show` of the page would carry has the id — `Borealis Dynamics
    (\`borealis-dynamics\`)`. `registry=None` (every OTHER caller of this module, and `company`
    scope, which never carries an id) falls back to the raw declared text unresolved.
    """
    anchoring = anchoring if isinstance(anchoring, dict) else {}
    if str(anchoring.get("kind", "")).lower() == "company":
        reason = _clean(anchoring.get("reason", ""), 200)
        return f"company-wide scope ({reason})" if reason else "company-wide scope"
    if registry is None:
        return _listed(anchoring.get("entities"))
    parts, seen_ids = [], set()
    for raw in _as_list(anchoring.get("entities")):
        cid = registry.canonical_id(raw)
        if cid:
            # Mirrors `gates.resolve_entity_ids`'s own dedup — two
            # declared spellings resolving to the SAME id (`["acme", "Acme Corp"]`) must read as
            # one entity here too, or the report says "Acme Corp (`acme`), Acme Corp (`acme`)"
            # right beside a page whose `entity:` frontmatter (deduplicated) carries the id once.
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
        if not cid:
            # This is the one path a FILED report (not a park) can print
            # text nobody has verified — `raw` came straight off captured material, and every
            # OTHER route to here runs only after `gate_anchoring` confirmed the declared value
            # resolves. Defensive (see this function's own docstring), but if it is ever reached,
            # it is an IDENTITY-shaped value from untrusted material and gets the stricter filter
            # `_clean_identity` already gives `triage_entity`'s name — not the plain-prose `_clean`.
            parts.append(f"`{_clean_identity(raw, 80)}`")
            continue
        name = registry.title(cid)
        cid_text = _clean(cid, 80)
        parts.append(f"{_clean(name, 120)} (`{cid_text}`)" if name else f"`{cid_text}`")
    return ", ".join(parts) if parts else NONE_LABEL


# ── filed ─────────────────────────────────────────────────────────────────────────────────────
def filed(*, page_path: str, commit: str, anchoring: dict, links: list, overlaps: list,
          findings: list, pages_edited: list = (), agent_rationale: str = "", registry=None,
          source_pages: list = ()) -> dict:
    """The ordinary success. Names the page, the commit and the anchor — and says, in the same
    sentence, that the brain cannot answer about it yet.

    `registry` is threaded straight to `_anchor_phrase`, so the anchor phrase names the same
    registry id the page's own `entity:` frontmatter was stamped with — see that function's
    docstring for why both the id and the display name are shown.


    `pages_edited` is every page OTHER than the filed one that this commit changed — the additive
    `related:` links and callouts `edits.py` applied from the agent's declaration. Reported because
    the commit touched somebody else's page: `processing` computed this list and discarded it, so a
    submitter's report could not answer "what else did my capture change", and neither could the
    operator reading `capture_queue`. `overlaps_flagged` is not the same field — it is the agent's
    JUDGMENT about which pages overlap; this is what code actually wrote.

    `agent_rationale` is the agent's own sentence about why this page went where it went
    (`Outcome.summary`). Every other field here is code's observation; this is the only one that
    answers "why", which is what lets a human check the JUDGMENT rather than only its output.

    `source_pages`, additive the same way: the source attachment's code-written part(s), in
    part order — the fast lane's slimmer sibling of `filed_meeting`'s field of the same name. One
    sentence in the summary and the structured list beside it, so neither a human nor a caller
    has to learn that a page rode in this commit from `git show`.
    """
    anchor = _anchor_phrase(anchoring, registry)
    summary = (f"{schema.FILED} — {_clean(page_path)}@{commit}, anchored to {anchor}. "
               f"{SEARCHABILITY_NOTE}")
    overlap_paths = [_path_of(o) for o in _as_list(overlaps)]
    if overlap_paths:
        # The near-duplicate variant must be visibly different from the exact-duplicate refusal:
        # this one FILED, and says what happened to both sides.
        others = _listed(overlap_paths)
        summary = (f"{schema.FILED} — {_clean(page_path)}@{commit}, anchored to {anchor}. "
                   f"This overlaps existing material at {others}; both pages now cross-link and "
                   f"carry an overlap note — nothing was deleted or rewritten on either side. "
                   f"{SEARCHABILITY_NOTE}")
    source_paths = [_clean(path, 200) for path in _as_list(source_pages)]
    if source_paths:
        summary += (f" The captured material itself is filed verbatim at "
                    f"{_listed(source_paths)}, and the page cites it in `sources:`.")
    return base_report(
        status=schema.FILED, summary=summary,
        page_path=page_path, commit=commit, anchored_to=anchor,
        links_created=[_clean(link, 120) for link in _as_list(links)],
        overlaps_flagged=[_clean(path, 200) for path in overlap_paths],
        pages_edited=[_clean(path, 200) for path in _as_list(pages_edited)],
        agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
        findings=list(_as_list(findings)),
        **({"source_pages": source_paths} if source_paths else {}))


# ── filed_meeting: the report for a page SET ──────────────────────────────────────────────────
def _reuse_lines(reuse: dict) -> list:
    """What happened to a distillation that had been parked, in the place a human actually reads.

    Empty for every ordinary filing (no stored outcome was involved), so the common report is
    unchanged. Two shapes otherwise:

    * **reused** — the parked pass's decisions filed unchanged, no re-reading. Worth one line
      because the alternative used to be silent knowledge loss, and an operator who sees a
      re-filed meeting should be able to tell which happened.
    * **re-distilled** — the model ran again, and this is the DIFF. It is the whole instrument:
      The loss it exists to surface was caught by hand, comparing one pass's six decisions
      against the next pass's three, with nothing in the system saying a word about it.
      `dropped` is listed FIRST and named plainly,
      because a decision that disappeared between two passes is the finding; `added` may be a
      genuine improvement or the same drift in the other direction, and the reader is the only one
      who can tell.
    """
    if not reuse:
        return []
    if reuse.get("reused"):
        titles = reuse.get("decisions") or []
        return ["",
                f"  reuse              re-filed the distillation from the parked pass — "
                f"{len(titles)} decision(s) preserved, the transcript was not read again"]
    dropped, added, kept = (reuse.get("dropped") or [], reuse.get("added") or [],
                           reuse.get("kept") or [])
    # Two ways to reach this branch, and the second is why the wording is about the CAPTURE rather
    # than about this pass: either the transcript really was read again here, or an
    # EARLIER pass re-read it, parked a smaller distillation, and this pass faithfully re-filed
    # that. In the second case no model ran on this pass and decisions are still missing, so a
    # sentence about what this pass did would be true and useless.
    head = ("the parked distillation could not be re-filed, so the transcript was read again."
            if reuse.get("model_ran", True) else
            "an EARLIER pass re-read the transcript and parked a smaller distillation; this pass "
            "re-filed that one.")
    lines = ["",
             f"  ⚠ RE-DISTILLED     {head} Against the FIRST park: {len(kept)} decision(s) "
             f"survived, {len(dropped)} did not, {len(added)} are new."]
    if dropped:
        lines.append(f"    DROPPED (was in the parked pass, is not being filed now): "
                     f"{_listed([_clean(t, 120) for t in dropped])}")
        lines.append("    Read those before accepting this filing — a decision that vanishes "
                     "between two passes is the failure this diff exists to surface.")
    if added:
        lines.append(f"    new (not in the parked pass): "
                     f"{_listed([_clean(t, 120) for t in added])}")
    return lines


def filed_meeting(*, source_pages: list, meeting_page: str, decisions: list, commit: str,
                  agent_rationale: str = "", registry=None, reuse: dict | None = None) -> dict:
    """`filed`'s sibling for a page SET: N >= 1 source pages, a meeting page, and N decision pages
    — each with its OWN anchor outcome (`gate_anchoring` judges each decision page
    individually). `decisions` is `[{"path": ..., "anchoring": {...}}, ...]`.

    **`source_pages` is a LIST, not a single path** — the source page is written verbatim by code
    and split, cross-linked, into N parts when the transcript is over the contract's line cap
    (`processing._build_source_parts`). Every part is listed; nothing is folded into one string.

    Reuses `_anchor_phrase` unmodified, once per decision page — no new anchor-rendering logic;
    the id-and-display-name pairing applies identically here, decision by decision.

    `result_ref` names the MEETING PAGE (`<meeting page path>@<sha>`) — the human's one door into
    the set, and what keeps `dedup.Match.page_path`'s existing `rsplit("@")` contract working
    unchanged. The FULL page list lives here, in the report, never folded into `result_ref`
    itself.
    """
    n = len(decisions)
    source_pages = list(source_pages)
    n_source = len(source_pages)
    source_label = "source page" if n_source == 1 else f"source page ({n_source} parts)"
    head = (f"{schema.FILED} — 1 {source_label}, 1 meeting page, {n} decision page(s), committed "
            f"as {_clean(commit)}. {SEARCHABILITY_NOTE}")
    lines = [head, "",
            f"  source page       {_listed([_clean(p) for p in source_pages])} (the transcript "
            f"— permanent evidence, never distilled)",
            f"  meeting page      {_clean(meeting_page)} (provenance — attendees, action items, "
            f"and links to the {n} decision(s) below; carries no anchor of its own)"]
    decision_rows = []
    if decisions:
        lines.append("  decision pages:")
        for index, d in enumerate(decisions, start=1):
            anchor = _anchor_phrase(d.get("anchoring") or {}, registry)
            lines.append(f"    {index}. {_clean(d.get('path', ''))} — anchored to {anchor}")
            decision_rows.append({"path": _clean(d.get("path", ""), 200), "anchored_to": anchor})
    else:
        lines.append("  decision pages    (none — nothing from this meeting was drafted as a "
                     "decision worth its own page)")
    lines.append(f"  links_created     {NONE_LABEL}")
    lines.append(f"  overlaps_flagged  {NONE_LABEL}")
    lines.append(f"  pages_edited      {NONE_LABEL}")
    lines.append(f"  agent_rationale   {_clean(agent_rationale, RATIONALE_WIDTH) or NONE_LABEL}")
    lines += _reuse_lines(reuse or {})
    return base_report(
        status=schema.FILED, summary="\n".join(lines), page_path=meeting_page, commit=commit,
        agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
        # The structured sibling of the rendered lines above — every page path and every decision's
        # anchor outcome, for a caller that reads the fact set rather than the prose.
        #
        # `distillation_reuse` rides beside it for the same reason: the rendered
        # lines are for the human, and a caller that wants to ASSERT on what changed between a
        # parked pass and this one needs the fact set, not the prose. Absent entirely on an
        # ordinary filing.
        filed_meeting={"source_pages": [_clean(p, 200) for p in source_pages],
                      "meeting_page": meeting_page, "decisions": decision_rows},
        **({"distillation_reuse": reuse} if reuse else {}))


def filed_retry(*, original_id: int, page_path: str, commit: str) -> dict:
    """Retry collapse. Reads as neither a fresh success nor a penalty: the material IS filed, at
    that page, and this row is the same capture arriving twice."""
    summary = (f"{schema.FILED} — same content as submission #{original_id}, already committed "
               f"as {_clean(page_path)}@{commit}; this row is a retry of that one, not a second "
               f"capture, so nothing new was written.")
    return base_report(status=schema.FILED, summary=summary, page_path=page_path, commit=commit,
                       retry_of=original_id)


# ── rejected ──────────────────────────────────────────────────────────────────────────────────
def _rejected(reason_code: str, summary: str, **facts) -> dict:
    """A refusal, with the machine-readable half of "why" beside the sentence.

    `reason_code` (`capture.schema.REJECTION_REASONS`) is what a READ path is allowed to branch on.
    Every `rejected` builder goes through here so none can ship without one: the code is not
    decoration, it is what `capture.queue`'s list surface consults to decide whether this row's
    captured material may be echoed back, and a refusal that forgot to carry one is a row the query
    has to fall back to withholding blind (`queue._MATERIAL_WITHHELD`, second clause).
    """
    return base_report(status=schema.REJECTED, summary=summary,
                       **{schema.REASON_CODE_KEY: reason_code}, **facts)


def rejected_duplicate(*, page_path: str, as_of: str) -> dict:
    """Exact duplicate of material already in the graph. Must NOT read like the near-duplicate
    success above — nothing was created, and the corrective action is different."""
    summary = (f"{schema.REJECTED} — this matches a page already in the graph: "
               f"{_clean(page_path)} (filed {as_of}); nothing new was created. If this capture "
               f"adds new information, resubmit just what's different.")
    return _rejected(schema.REASON_DUPLICATE, summary, page_path=page_path,
)


def rejected_secret(*, line: str, rule_id: str, where: str = "your material") -> dict:
    """A gitleaks hit. Names the kind and a locator, never the value, and never blames.

    **The sentence promises what the read path now delivers, and no more.** It used to say the
    value was "not included here or in any log" while `brain_submissions` served a 500-character
    excerpt of the same material back — to the submitter on every call, and to any steward listing
    everybody's captures. The excerpt is suppressed at the query (`queue._MATERIAL_WITHHELD`, keyed
    on this report's `reason_code`), which is what makes the first clause true.

    **The material is NOT "archived exactly as submitted".** A secret/PII rejection purges
    `payload`/`hints` IMMEDIATELY (`retention.purge_secret_capture_immediately`, called from
    `worker._finish` right after this row lands `rejected`) rather than waiting on the ordinary
    30-day window — the one rejection reason for which that window is the wrong clock. The
    sentence says so plainly rather than promising a retention that does not happen: a real
    credential still needs rotating (this report is not proof gitleaks was wrong), but "stays
    archived… rotate it" would be false.
    """
    # An empty `line` is not a missing value: it means the match only appeared once adjacent lines
    # were rejoined, so the credential is broken across a line break in the submitted material and
    # no single line number would send anyone to the right place. Saying which is more useful than
    # a number that does not point at it.
    located = (f"near line {line} of {where}" if line
               else f"in {where}, split across a line break")
    summary = (f"{schema.REJECTED} — gitleaks matched a likely secret {located} "
               f"(rule: {_clean(rule_id, 80)}); the value is not repeated in this report, "
               f"in any log, or in any read-back of this capture. Remove it and resubmit — nothing "
               f"was filed and no partial page exists. Your captured material has been purged "
               f"immediately because of this match; if that was a live credential, rotate it "
               f"regardless of this report.")
    return _rejected(schema.REASON_SECRET, summary)


def rejected_pii(*, line: str, pattern_label: str, where: str = "your material") -> dict:
    """The PII variant. Same shape, same safety: the KIND and a locator only — and the same two
    corrections `rejected_secret` carries, for the same reason (see its docstring: the material
    is purged immediately, not on the ordinary 30-day window)."""
    summary = (f"{schema.REJECTED} — what looks like {pattern_label} was found near line {line} "
               f"of {where}; the value is not repeated in this report, in any log, or in any "
               f"read-back of this capture. Remove it and resubmit — nothing was filed and no "
               f"partial page exists. Your captured material has been purged immediately because "
               f"of this match.")
    return _rejected(schema.REASON_PII, summary)


def rejected_steering(*, path: str, category: str, findings: list = ()) -> dict:
    """The diff veto WITH a traceable steering attempt in the captured material.

    Content-actionable on purpose: something in the material tried to move the librarian out of
    its lane and code caught it, so the submitter can act. The counterpart — the same veto on
    ORDINARY material — is a `failed`, because the benign-twin rule says this must never fire on
    ordinary content, and telling that person to "fix and resubmit" sends them looping against a
    bug (see `failed_system`).
    """
    summary = (f"{schema.REJECTED} — your material tried to make the librarian write outside "
               f"the lane (category: {category}) and the attempt reached {_clean(path)}; "
               f"nothing was filed and no partial page exists. Remove the instruction-like text "
               f"and resubmit the content you actually want kept.")
    return _rejected(schema.REASON_STEERING, summary,                     findings=list(findings))


def rejected_malformed_frontmatter(*, findings: list = ()) -> dict:
    """The stamped page's frontmatter could not be parsed as valid YAML —
    almost always a list-shaped field (`entity:`, `acl:`, `related:`...) whose continuation lines
    were not indented under its key. Content-caused, not the librarian failing at its job:
    `processing._frontmatter_only` only routes here when that finding is the WHOLE veto, and
    the fix is on the submitter's side (reshape the field), not something a retry or a steward can
    repair on the page's behalf."""
    summary = (f"{schema.REJECTED} — the frontmatter in your material could not be turned into a "
               f"valid page; nothing was filed and no partial page exists. This usually means a "
               f"list-shaped field (`entity:`, `acl:`, `related:`, ...) was written across "
               f"multiple lines without the continuation indented under its key. Resubmit with "
               f"that field as a single-line list, e.g. `entity: [\"acme\"]`, or with its "
               f"continuation lines indented under the key.")
    return _rejected(schema.REASON_MALFORMED_FRONTMATTER, summary,                     findings=list(findings))


def rejected_forged_field(*, findings: list = ()) -> dict:
    """Findings cycle 2, B5: `gate_frontmatter`'s other two finding codes — `forged-field`
    (the filed page declares a server-owned field with a value the server did not stamp, or a
    field like `entity` more than once, or a raw construct like a BOM/explicit-key line this
    dialect refuses outright) and `forbidden-field` (`owner`/`id`/`content_hash`, never legitimate
    on a fast-lane page at all) — SAME gate, same cause class as `unparseable`: the librarian could
    not finish because of something the MATERIAL declared, not because it failed at its job. 4.7
    reclassified `unparseable` alone from `failed` to `rejected` for exactly this reasoning and left
    its two siblings routing to `failed` — "the librarian is broken" — for material that tried to
    assert `owner`/`verification`/`entity`/etc. itself. `processing._frontmatter_only` routes here
    when either code is present (alongside `unparseable` or not) and is the WHOLE veto; the SAME
    `REASON_MALFORMED_FRONTMATTER` reason code as the sibling builder above, because a read path may
    only branch on the code, and "the frontmatter gate refused this page" is one cause class with
    two different explanations, not two different reasons."""
    summary = (f"{schema.REJECTED} — your material declared a frontmatter field it may not "
               f"assert: either one the server computes itself (`owner`, `acl`, `entity`, "
               f"`content_hash`, `id`) or `verification`, which nothing computes since the trust "
               f"layer was removed and which therefore no page may claim. Nothing was filed and "
               f"no partial page exists. Remove that field from what you submit and resubmit — "
               f"the librarian fills in what it computes.")
    return _rejected(schema.REASON_MALFORMED_FRONTMATTER, summary,                     findings=list(findings))


# ── needs_input: the one question a capture gets ──────────────────────────────────────────────
# The two outcomes the reader is offered, and the consequence of each, written once because they
# are restated at three touchpoints on purpose (this question, `brain_reply`'s acknowledgement, the
# tester briefing): the one-ask budget is exactly the rule a confused reader tests by replying
# twice or by waiting for a follow-up that will never come.
ONE_ASK_CLAUSE = ("This is the only question this capture gets: if your answer still can't be "
                  "matched to a registered entity, it goes to a steward too, rather than asking a "
                  "second time.")

# What a candidate with no aliases says, rather than a blank parenthesis. Same "nothing is silently
# omitted" rule the field block above follows — an empty bracket reads as a rendering fault.
NO_ALIASES = "no other names on file"


def _candidate_lines(candidates) -> list[str]:
    """One `- Name (also known as: …)` line per registry entity, bounded and sanitized.

    A registry entry is curated, not captured — but it arrives here through a JSON file in a repo
    the librarian does not own, so it crosses the same `_clean` seam as everything else that reaches
    a person. A newline in a name would otherwise forge this list's own structure, and the list sits
    directly above the line that states a command.
    """
    lines = []
    for candidate in _as_list(candidates):
        name = _clean((candidate or {}).get("name", ""), 120)
        if not name:
            continue
        aliases = [_clean(a, 80) for a in _as_list((candidate or {}).get("aliases")) if _clean(a)]
        lines.append(f"  - {name} (also known as: {', '.join(aliases) or NO_ALIASES})")
    return lines


def needs_input(*, submission_id, name: str, candidates=(), total_candidates: int | None = None,
                agent_rationale: str = "", findings: list = ()) -> dict:
    """The librarian's one question, CODE-BUILT rather than agent prose.

    Every fact in it is code's observation — the name the agent could not resolve, the registry
    this run actually loaded, and the exact call that answers it. The agent's own reading of the
    material travels beside it as `agent_rationale`, under a name that says whose claim it is,
    exactly as on every other report.

    **Three shapes, and the difference between them is which situation the reader is actually in**
    — a distinction `anchoring_brief` already makes for the agent, for the same reason: "nothing is
    registered yet" and "your name is not among these five" are different problems, and a message
    that renders them alike leaves the reader to discover the difference by failing.

    - **registry small enough to show**: the whole registry, with aliases.
    - **registry empty**: says so, and says that outcome 1 does not exist on this pass — there is
      nothing to match against, so "it's new" is the only truthful answer available.
    - **registry too large to list**: names the count and asks for the exact name. It shows NO
      list rather than a ranked subset — see `MAX_QUESTION_CANDIDATES` for why a filtered list is
      the more dangerous shape.

    **It is a message for a human, never a repair brief for an agent**: no gate vocabulary, no
    "the gate examined N wikilinks", no JSON. The
    agent-facing counterpart of this same situation is `gates.anchoring_brief`, and the two share a
    situation, a registry and nothing else — they have different readers and must not share a
    template.

    The reply invocation is on its OWN LINE, introduced by a fixed phrase, and also travels as a
    structured field (`reply_invocation`, surfaced as `reply_hint` by `brain_submissions`). Both,
    not either: this message is often read through the reader's own LLM session rather than raw,
    and a paraphrase that helpfully compresses `brain_reply(...)` into "just reply with your answer"
    would leave the promise true of the row and false of what the person saw.
    """
    clean_name = _clean(name, 120)
    invocation = schema.reply_invocation(submission_id)
    lines = _candidate_lines(candidates)
    total = len(_as_list(candidates)) if total_candidates is None else int(total_candidates)

    head = (f"{schema.NEEDS_INPUT} — capture #{submission_id} is parked on one question before it "
            f"can be filed: your material seems to be about \"{clean_name}\", and the entity "
            f"registry doesn't recognize that name.")
    if not total:
        body = (f"{schema.NEEDS_INPUT} — capture #{submission_id} is parked on one question before "
                f"it can be filed: your material seems to be about \"{clean_name}\", and nothing is "
                f"registered in the entity registry yet — there is nothing to match it against.\n\n"
                f"Reply saying it's new (or, if you think it should already be registered, say what "
                f"you expected) — a steward takes it from there either way; your material stays "
                f"archived until they do. This is the only question this capture gets.")
    elif lines:
        body = (f"{head} Here is everything registered today:\n\n"
                + "\n".join(lines)
                + "\n\nReply naming one of these exactly if your material is about it. If it's new, "
                  "or you're not sure, say so — a steward takes it from there; your material stays "
                  f"archived either way. {ONE_ASK_CLAUSE}")
    else:
        body = (f"{head} The registry has {total} entities registered today — too many to list "
                f"here.\n\nAnswer with the exact name of whatever your material is actually about "
                f"and we'll match it, aliases included. If it's new, or you're not sure, say so — a "
                f"steward takes it from there; your material stays archived either way. "
                f"{ONE_ASK_CLAUSE}")

    summary = f"{body}\n\nReply with:\n  {invocation}"
    return base_report(status=schema.NEEDS_INPUT, summary=summary,
                       agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
                       findings=list(_as_list(findings)),
                       open_question=f"which entity is {clean_name}?",
                       # The command as a FACT beside the sentence that states it, mirroring
                       # `reason_code`'s place beside a refusal: prose is for the person, this is
                       # what a reader that branches — or a renderer that must not truncate it —
                       # can rely on.
                       reply_invocation=invocation,
                       unresolved_name=clean_name)


# ── needs_input_multi: several unresolved names, ONE ask ──────────────────────────────────────
# `needs_input` above stays BYTE-IDENTICAL for the single-name case — every non-meeting caller of
# it keeps seeing exactly what it always has. This is a SIBLING, used only when a capture has more
# than one unresolved name (a meeting naming two customers and an unregistered project code).
ONE_ASK_CLAUSE_MULTI = (
    "This is the only question this capture gets, for all {n} at once: if even one of them is "
    "still unplaced after your reply, the whole meeting parks for a steward — not just the "
    "decision that names it, because a meeting page can never link a decision that was never "
    "filed.")


def _numbered_names(names: list[str]) -> str:
    return "\n".join(f'  {i}. "{_clean(name, 120)}"' for i, name in enumerate(names, start=1))


def needs_input_multi(*, submission_id, names: list[str], candidates=(),
                      total_candidates: int | None = None, agent_rationale: str = "",
                      findings: list = ()) -> dict:
    """`needs_input`'s plural sibling. Every unresolved name is listed, numbered, UNCAPPED
    (report.py's own `MAX_QUESTION_CANDIDATES` doctrine — "never a silently truncated candidate
    list" — extended here to the unresolved-names field: every one of them is something the reply
    is REQUIRED to place, so capping the list would be actively wrong, not merely inconvenient).
    """
    names = [_clean(n, 120) for n in _as_list(names) if _clean(n, 120)] or ["something unnamed"]
    n = len(names)
    invocation = schema.reply_invocation(submission_id)
    lines = _candidate_lines(candidates)
    total = len(_as_list(candidates)) if total_candidates is None else int(total_candidates)
    numbered = _numbered_names(names)
    clause = ONE_ASK_CLAUSE_MULTI.format(n=n)

    head = (f"{schema.NEEDS_INPUT} — capture #{submission_id} is parked on one question before it "
            f"can be filed: your material names {n} things the entity registry doesn't "
            f"recognize:\n\n{numbered}\n")
    if not total:
        body = (f"{head}\nNothing is registered in the entity registry yet, so there is nothing "
                f"to match any of them against.\n\nReply saying, for each of the {n}, that it's "
                f"new (or, if you think one should already be registered, say what you expected) "
                f"— a steward takes it from there either way; your material stays archived until "
                f"they do. {clause}")
    elif lines:
        body = (f"{head}\nHere is everything registered today:\n\n" + "\n".join(lines)
               + f"\n\nReply once, covering all {n}: for each name above, say which registered "
                 f"entity it is, that it's new, or that you're not sure — a steward takes over any "
                 f"you can't place; your material stays archived either way. {clause}")
    else:
        body = (f"{head}\nThe registry has {total} entities registered today — too many to list "
               f"here.\n\nAnswer with the exact name of whatever each of the {n} is actually "
               f"about and we'll match it, aliases included; for any that's new, or you're not "
               f"sure, say so — a steward takes it from there. {clause}")

    summary = f"{body}\n\nReply with:\n  {invocation.replace('<your answer>', f'<your answer, covering all {n}>')}"
    return base_report(status=schema.NEEDS_INPUT, summary=summary,
                       agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
                       findings=list(_as_list(findings)),
                       open_question=f"which entities are {', '.join(names)}?",
                       reply_invocation=invocation, unresolved_names=names)


def triage_entity_multi(*, names: list[str], agent_rationale: str = "", findings: list = (),
                        asked: bool = False) -> dict:
    """`triage_entity`'s plural sibling — the parked-meeting report naming several unresolved
    names at once. `asked=True`'s tail is `triage_entity`'s own unchanged
    sentence, reused verbatim: it is already written to be true regardless of how many names the
    one question covered.

    **Which key this writes, recorded here where a future reader will find it**: it writes
    `schema.SITUATION_NAMES_KEY` (a list), NOT the singular
    `SITUATION_NAME_KEY` — see that key's own docstring in `capture.schema` for the full decision
    and for how `entities.situations`/`entities.cli._print_next_commands` read it per name,
    independently, so a steward approving one name is never blocked by another failing
    `_suggestable`.
    """
    clean_names = [_clean_identity(n, 120) for n in _as_list(names) if _clean_identity(n, 120)] \
        or ["something unnamed"]
    n = len(clean_names)
    quoted = [f'"{name}"' for name in clean_names]
    numbered = quoted[0] if n == 1 else ", ".join(quoted[:-1]) + f" and {quoted[-1]}"
    tail = ("You already answered the one question this capture gets, and the answer still "
            "doesn't match a registered entity — so a steward takes it from here and you won't be "
            "asked again. Nothing further is needed from you; your material stays archived until "
            "it's reviewed." if asked else
            "Nothing further is needed from you — no question is coming about this one; your "
            "material stays archived until it's reviewed.")
    summary = (f"{schema.TRIAGE} — parked, not filed. Your material named {n} things the entity "
               f"registry doesn't recognize — {numbered} — and at least one of them still "
               f"doesn't match a registered entity. A steward will register whichever of these "
               f"are new, or place this meeting where it actually belongs. {tail}")
    return base_report(status=schema.TRIAGE, summary=summary,
                       agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
                       findings=list(_as_list(findings)), asked=bool(asked),
                       **{schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                          schema.SITUATION_NAMES_KEY: clean_names},
                       open_question=f"which entities are {', '.join(clean_names)}?")


# ── triage ────────────────────────────────────────────────────────────────────────────────────
def triage_entity(*, name: str, agent_rationale: str = "", findings: list = (),
                  asked: bool = False) -> dict:
    """Parked because the thing it is about is not a registered entity. One of TWO triage
    flavors that must not collapse into one string: this one may resolve later.

    Carries `agent_rationale` for the same reason `filed` does, and arguably a stronger one: a park
    is entirely a judgment, so the steward who picks this up needs the agent's reading of the
    material, not only the name it could not resolve.

    **Three paths reach this sentence now, and it is written to be true of all of them.** The agent
    decides it cannot anchor and parks the capture on a capture whose one question is already spent
    (`processing._triage`); it tries, the anchoring gate refuses the attempt on both passes, and
    `processing._unanchorable` parks it instead; or the submitter ANSWERED and the answer still
    resolves to nothing. The news is the same in every case — a name nothing registers, no page, no
    commit, a steward's call.

    **`asked` is the one thing the sentence does distinguish**, and it has to. This function
    must never tell a submitter that no follow-up mechanism exists: the follow-up DOES exist, and
    this row is here either because the capture never qualified for one or because it already used
    it. Telling someone who has just replied that no such mechanism exists would read as their
    answer having gone nowhere.

    `findings` travels for the same reason it does on every other terminal state: a parked capture
    whose material tried to steer the librarian must record the attempt like a filed or rejected one
    does.
    """
    # `_clean_identity`, not `_clean`: this value is an entity NAME, and it is the one field in
    # this module a steward is later invited to paste into a shell command (`stigmergy-entities
    # show`). See the constant for why the filter lives here as well as there.
    clean_name = _clean_identity(name, 120)
    tail = ("You already answered the one question this capture gets, and the answer still doesn't "
            "match a registered entity — so a steward takes it from here and you won't be asked "
            "again. Nothing further is needed from you; your material stays archived until it's "
            "reviewed." if asked else
            "Nothing further is needed from you — no question is coming about this one; your "
            "material stays archived until it's reviewed.")
    summary = (f"{schema.TRIAGE} — parked, not filed. Your material seems to be about "
               f"\"{clean_name}\", which the entity registry doesn't recognize yet, so it can't "
               f"be anchored. A steward will register {clean_name} as a new entity or place "
               f"this where it actually belongs. {tail}")
    return base_report(status=schema.TRIAGE, summary=summary,
                       agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
                       findings=list(_as_list(findings)),
                       asked=bool(asked),
                       # The code and the name beside the sentence, never only inside it
                       # (`schema.SITUATION_KEY` carries the argument): `stigmergy-entities` selects
                       # the rows a steward can `approve` on this key, and the name it pre-fills
                       # `--name` with comes from here rather than from parsing `open_question`.
                       **{schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                          schema.SITUATION_NAME_KEY: clean_name},
                       open_question=f"which entity is {clean_name}?")


def triage_type(*, judged_type: str, agent_rationale: str = "", findings: list = ()) -> dict:
    """Parked because the fast lane does not file that type. The other triage flavor: as
    submitted, this one never resolves — the material has to go through a different door.

    **The label carries its own article**, and both sentences below are written for that.
    `This reads like {label} material` rendered "This reads like a meeting material" — the grammar of
    a template that assumed a bare noun, in a submitter-facing sentence, and wrong for every one of
    the 17 labels rather than only for `meeting`. The label is now dropped in whole ("This reads like
    a meeting page"), which reads correctly for every row of `page.PAGE_TYPES`.

    An UNKNOWN type has no label, and there is no way to article a value an agent invented
    (`a incident`), so the fallback is a MASS noun — "recipe material" — which needs no article and
    reads correctly in both sentences here. That is why `material` did not simply move into the label
    table: carrying it is the fallback's job, not every label's.

    **Two paths reach this sentence as well**, and it is written to be true of both: the agent
    recognises the governed type and parks the capture (`processing._triage`), or it writes the page
    anyway and the zone gate refuses the type it minted (`processing._uncreatable_type`). The
    judged type is the same fact either way — the folder the page landed in and the type the agent
    declared are the same value by then, because the zone gate refuses any page where they differ.
    `findings` travels for the same reason it does on `triage_entity`.
    """
    # `or "unknown"` is the SAME fallback word `processing._triage` uses for a parked outcome with no
    # `judged_type` at all, so a missing field renders one way rather than two: without it an empty
    # value reached the sentence as "This reads like  material", with the hole where the news was.
    clean_type = _clean(judged_type, 60) or "unknown"
    # Both the label and the list of what the fast lane DOES file come from `page.PAGE_TYPES` —
    # the one table. A seventh fast-lane type must not leave this sentence claiming there are six.
    label = page_policy.label_for(clean_type) or f"{clean_type} material"
    fast_lane = page_policy.FAST_LANE_TYPE_LIST
    count = len(page_policy.FAST_LANE_TYPES)
    summary = (f"{schema.TRIAGE} — parked, not filed. This reads like {label}, and the "
               f"fast lane only files {count} page types ({fast_lane}); {clean_type} needs a "
               f"steward's review before it can exist here. Nothing was written to the graph; "
               f"your material stays archived.")
    return base_report(status=schema.TRIAGE, summary=summary,
                       agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
                       findings=list(_as_list(findings)),
                       # Same fact-beside-the-sentence rule as `triage_entity` above. This flavor
                       # reaches `stigmergy-entities` too: `person`/`team`/`product` material is an
                       # identity situation a steward may answer by minting the entity.
                       **{schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                          schema.SITUATION_TYPE_KEY: clean_type},
                       open_question=f"where does {label} belong?")


# ── failed ────────────────────────────────────────────────────────────────────────────────────
def failed_system(*, attempts: int, stage: str, reason: str, agent_attempts: int = 0,
                  cost_usd: float = 0.0, findings: list = ()) -> dict:
    """The librarian could not comply. Deliberately a different sentence SHAPE from every
    `rejected` above: there is nothing for the submitter to fix.

    Two things this used to get wrong, both from the same walk:

    - it asserted that **resubmitting would hit the same fault**. The agent is not deterministic,
      so a retry may well succeed; and the case that produced this message — the agent stepping out
      of its lane and the zone gate refusing it — is neither the submitter's fault nor a
      reproducible system fault. It now says what is true: the librarian could not comply, nothing
      was filed, and here is what actually happens next.
    - it reported `attempts`, the QUEUE DELIVERY counter, in a sentence a reader takes to mean the
      agent's tries ("after 1 attempts" while the agent had had two). Both numbers are now present
      and each says which one it is, so an operator can tell whether the corrective retry ran.

    **And a third: it was the ONE terminal builder that took no `findings`.** Every other one —
    `filed`, `_rejected`'s family, `triage_entity`, `triage_type`, `needs_input` — threads them into
    `base_report`, which is the dict `queue.finish` persists to `capture_queue.report` and
    `brain_submissions` hands back. `_refuse`/`_refuse_meeting` compose the injection notes for
    EVERY road they can take and then reached this one, where the notes went onto `Result.findings`
    — a field nothing persists — and vanished. So a capture whose material tried to steer the
    librarian AND which then hit a system fault recorded the steering attempt nowhere, which is the
    same "two roads to one destination disagree about what happened" defect `processing._triage`'s
    own docstring records having fixed for the parked road. The parameter has a default, so the
    fault road that genuinely has no findings (`processing.failure_result`, raised from anywhere in
    the path including before the agent ran) is unchanged.
    """
    inside = (f", {_plural(agent_attempts, 'agent attempt')} inside it"
              if agent_attempts else "")
    summary = (f"{schema.FAILED} — the librarian could not finish this "
               f"(queue delivery {attempts}{inside}; last problem: {_clean(stage, 40)} — "
               f"{_clean(reason, 200)}); nothing was filed and nothing was committed. Your "
               f"material is fine and is still archived — this is the librarian failing, not your "
               f"capture. Nothing further happens automatically: a steward needs to look at the "
               f"fault. Resubmitting may work, since the librarian is not deterministic, but it "
               f"will not fix the fault.")
    return base_report(status=schema.FAILED, summary=summary,
                       stage=stage, deliveries=attempts, agent_attempts=agent_attempts,
                       cost_usd=round(cost_usd, 6),
                       findings=list(_as_list(findings)))


# ── findings ──────────────────────────────────────────────────────────────────────────────────
def injection_finding(category: str) -> str:
    """The one sentence an injection attempt produces. Names a CATEGORY from the fixed set and
    never a substring of the planted instruction — a report that reproduces the payload is a
    second copy of the injection, delivered to a human."""
    return (f"finding: material attempted to instruct the librarian directly "
            f"(category: {category}) — not followed; filed as ordinary content only.")


# ── the second renderer: prose for a terminal ─────────────────────────────────────────────────
def render_prose(report: dict) -> str:
    """The CLI's rendering of the same fact set. The summary sentence is reused verbatim — the
    CLI never composes wording of its own — and the always-present fields follow it.

    `agent_rationale` is rendered on the FILED path unconditionally (as `(none)` when the agent
    wrote nothing), by the same "nothing is silently omitted" rule as the fields above it, and on
    the parked paths only when there is one — those reports carry no field block of their own, and a
    lone `(none)` under a parked summary would announce an absence rather than report an outcome.
    """
    lines = [report.get("summary", "")]
    status = report.get("status")
    is_meeting = "filed_meeting" in report
    if status == schema.FILED and not is_meeting:
        # `filed_meeting` already renders its own field block INTO `summary` — a page SET's
        # fields (per-decision anchors) do not fit the single `anchored_to`/one-page shape below,
        # so appending these generic lines again would duplicate what the summary already says.
        lines.append(f"  links_created    {_listed(report.get('links_created'))}")
        lines.append(f"  overlaps_flagged {_listed(report.get('overlaps_flagged'))}")
        lines.append(f"  pages_edited     {_listed(report.get('pages_edited'))}")
        lines.append(f"  agent_rationale  {report.get('agent_rationale') or NONE_LABEL}")
    elif status == schema.FILED:
        # The meeting case, explicit rather than a fallthrough: `filed_meeting`'s summary already
        # carries its own `agent_rationale` line (and every other field above), so there is
        # nothing left to append — this branch exists so that fact is a decision, not an accident
        # of which `elif` happens to catch a FILED report the first branch excluded.
        pass
    elif report.get("agent_rationale"):
        lines.append(f"  agent_rationale  {report['agent_rationale']}")
    for finding in report.get("findings", []):
        lines.append(f"  ! {finding}")
    return "\n".join(lines)
