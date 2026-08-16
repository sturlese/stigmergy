"""What a person is told about their capture — one fact set, rendered two ways.

Every terminal state composes its sentence HERE: `brain_submissions` returns the structured facts,
`stigmergy-librarian once` prints prose, and neither composes wording of its own. The rules: every
sentence names the literal enum value; never echo the offending value — locators, rule ids and
categories only; a refusal carries a `reason_code`, the only thing a READ path may branch on;
`failed` never shares a sentence shape with `rejected`; a number names which number it is (queue
DELIVERIES vs AGENT attempts); empty renders as `(none)`; echoed values go through `text.sanitize`.
"""
from stigmergy import text as textutil
from stigmergy.capture import schema
from stigmergy.librarian import page as page_policy

NONE_LABEL = "(none)"

# Wider than the 200-character clamp the decorative fields get: this field's CONTENT is the point.
RATIONALE_WIDTH = 400

# Re-exported from `capture.schema`: `capture` may not import `librarian`, so the vocabulary two
# packages must agree on sits with the column that stores it.
SEARCHABILITY_NOTE = schema.SEARCHABILITY_NOTE
base_report = schema.base_report

# Never a silently truncated candidate list: below the cutover the full registry, above it NONE
# plus the count. A ranked subset reads as "not registered" to someone who cannot know it was cut.
MAX_QUESTION_CANDIDATES = 20


def _clean(text: str, width: int = 0) -> str:
    """Untrusted text on its way to a human — `stigmergy.text.clamp` + `sanitize`, the same seam
    `capture.cli._clean` uses, so the two packages' renderers cannot disagree about truncation."""
    return textutil.clamp(textutil.sanitize(str(text or "")).replace("\n", " "), width)


# An IDENTITY field is not prose: `schema.SITUATION_NAME_KEY` comes off captured material and is
# offered as the `--name` of a shell command, and `sanitize` strips control characters only. Stated
# rather than imported because `entities` imports `librarian` and never the reverse.
_UNSAFE_IN_IDENTITY = set('/\\:*?"<>|[]#^') | set("'`$;&(){}!~\n\r\t")


def _clean_identity(text: str, width: int = 0) -> str:
    """`_clean`, then shell/filename metacharacters stripped; for NAMES, never prose. Stripped
    rather than refused: a capture must always be parkable, or a hostile name becomes a lost row."""
    return _clean("".join(c for c in str(text or "") if c not in _UNSAFE_IN_IDENTITY), width)


def _plural(count: int, singular: str, plural: str = "") -> str:
    """`1 attempt` / `2 attempts`. A message that says "1 attempts" tells a reader nobody read it."""
    return f"{count} {singular if abs(count) == 1 else (plural or singular + 's')}"


def _listed(values) -> str:
    return ", ".join(_clean(v, 120) for v in _as_list(values)) if values else NONE_LABEL


def _as_list(value) -> list:
    """Anything into a list, without raising. These functions run AFTER the commit and the push, so
    a `TypeError` here would leave the page on `main` and the row `failed`."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _path_of(entry) -> str:
    """The `path` of an overlap entry, whichever shape it arrived in."""
    return str(entry.get("path", "")) if isinstance(entry, dict) else str(entry)


def _anchor_phrase(anchoring: dict, registry=None) -> str:
    r"""`anchored to X`, for both anchoring outcomes. The page's `entity:` is stamped with the
    RESOLVED id, so this phrase spells it the same way; `registry=None` falls back to raw text."""
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
            # Mirrors `gates.resolve_entity_ids`'s dedup: two spellings of one id read as one.
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
        if not cid:
            # The one path a FILED report prints unverified text, so `_clean_identity` not `_clean`.
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
    """The ordinary success: page, commit and anchor, plus the not-searchable-yet clause in the same
    sentence. `pages_edited` is what code actually wrote from the agent's declared edits, while
    `overlaps_flagged` is the agent's JUDGMENT about which pages overlap — a different field."""
    anchor = _anchor_phrase(anchoring, registry)
    summary = (f"{schema.FILED} — {_clean(page_path)}@{commit}, anchored to {anchor}. "
               f"{SEARCHABILITY_NOTE}")
    overlap_paths = [_path_of(o) for o in _as_list(overlaps)]
    if overlap_paths:
        # Visibly different from the exact-duplicate refusal: this one FILED.
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
    """What happened to a distillation that had been parked; empty for every ordinary filing. Two
    shapes otherwise: reused (the parked decisions filed unchanged) and re-distilled (the DIFF).
    `dropped` is listed FIRST — a decision that disappeared between two passes is the finding."""
    if not reuse:
        return []
    if reuse.get("reused"):
        titles = reuse.get("decisions") or []
        return ["",
                f"  reuse              re-filed the distillation from the parked pass — "
                f"{len(titles)} decision(s) preserved, the transcript was not read again"]
    dropped, added, kept = (reuse.get("dropped") or [], reuse.get("added") or [],
                           reuse.get("kept") or [])
    # Two ways reach this branch, so the wording is about the CAPTURE rather than this pass: the
    # transcript was read again here, or an earlier pass re-read it and parked a smaller one.
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
    """`filed`'s sibling for a page SET: N >= 1 source pages, a meeting page, and N decision pages,
    each with its OWN anchor outcome. `decisions` is `[{"path": ..., "anchoring": ...}]`, and
    `result_ref` names the MEETING PAGE alone or `dedup.Match.page_path`'s `rsplit("@")` breaks."""
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
        # The structured sibling of the rendered lines above, for a caller that branches on facts.
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
    """A refusal. `capture.queue`'s list surface consults `reason_code` to decide whether this row's
    material may be echoed back, so every `rejected` builder goes through here to get one."""
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
    """A gitleaks hit: the kind and a locator, never the value. The sentence's promises are kept by
    `queue._MATERIAL_WITHHELD` and `retention.purge_secret_capture_immediately`."""
    # An empty `line` is not a missing value: the match appeared only once adjacent lines were
    # rejoined, so the credential straddles a line break and no line number would point at it.
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
    """The PII variant. Same shape, same safety: the KIND and a locator only, and the same
    immediate purge `rejected_secret` names."""
    summary = (f"{schema.REJECTED} — what looks like {pattern_label} was found near line {line} "
               f"of {where}; the value is not repeated in this report, in any log, or in any "
               f"read-back of this capture. Remove it and resubmit — nothing was filed and no "
               f"partial page exists. Your captured material has been purged immediately because "
               f"of this match.")
    return _rejected(schema.REASON_PII, summary)


def rejected_steering(*, path: str, category: str, findings: list = ()) -> dict:
    """The diff veto WITH a traceable steering attempt, so the submitter can act. The same veto on
    ORDINARY material is a `failed`: "fix and resubmit" would loop that person against a bug."""
    summary = (f"{schema.REJECTED} — your material tried to make the librarian write outside "
               f"the lane (category: {category}) and the attempt reached {_clean(path)}; "
               f"nothing was filed and no partial page exists. Remove the instruction-like text "
               f"and resubmit the content you actually want kept.")
    return _rejected(schema.REASON_STEERING, summary,                     findings=list(findings))


def rejected_malformed_frontmatter(*, findings: list = ()) -> dict:
    """The stamped page's frontmatter is not valid YAML — content-caused, so routed here only when
    that finding is the WHOLE veto and the fix is the submitter's."""
    summary = (f"{schema.REJECTED} — the frontmatter in your material could not be turned into a "
               f"valid page; nothing was filed and no partial page exists. This usually means a "
               f"list-shaped field (`entity:`, `acl:`, `related:`, ...) was written across "
               f"multiple lines without the continuation indented under its key. Resubmit with "
               f"that field as a single-line list, e.g. `entity: [\"acme\"]`, or with its "
               f"continuation lines indented under the key.")
    return _rejected(schema.REASON_MALFORMED_FRONTMATTER, summary,                     findings=list(findings))


def rejected_forged_field(*, findings: list = ()) -> dict:
    """`gate_frontmatter`'s `forged-field` and `forbidden-field` codes. Carries the sibling's
    `REASON_MALFORMED_FRONTMATTER` deliberately: a read path branches on the code, one cause."""
    summary = (f"{schema.REJECTED} — your material declared a frontmatter field it may not "
               f"assert: either one the server computes itself (`owner`, `acl`, `entity`, "
               f"`content_hash`, `id`) or `verification`, which nothing computes since the trust "
               f"layer was removed and which therefore no page may claim. Nothing was filed and "
               f"no partial page exists. Remove that field from what you submit and resubmit — "
               f"the librarian fills in what it computes.")
    return _rejected(schema.REASON_MALFORMED_FRONTMATTER, summary,                     findings=list(findings))


# ── needs_input: the one question a capture gets ──────────────────────────────────────────────
# Written once because it is restated at three touchpoints: this question, `brain_reply`'s
# acknowledgement, and the tester briefing.
ONE_ASK_CLAUSE = ("This is the only question this capture gets: if your answer still can't be "
                  "matched to a registered entity, it goes to a steward too, rather than asking a "
                  "second time.")

# Rather than a blank parenthesis — the "nothing is silently omitted" rule.
NO_ALIASES = "no other names on file"


def _candidate_lines(candidates) -> list[str]:
    """One `- Name (also known as: …)` line per registry entity, bounded and sanitized: a newline in
    a curated name would forge this list's structure, which sits directly above a stated command."""
    lines = []
    for candidate in _as_list(candidates):
        name = _clean((candidate or {}).get("name", ""), 120)
        if not name:
            continue
        aliases = [_clean(a, 80) for a in _as_list((candidate or {}).get("aliases")) if _clean(a)]
        lines.append(f"  - {name} (also known as: {', '.join(aliases) or NO_ALIASES})")
    return lines


# ── several unresolved names, still ONE ask ───────────────────────────────────────────────────
# ONE builder serves both counts and BOTH flows, so the base clause names no transcript. What is at
# stake IS flow-specific, and that half lives in `MEETING_CONSEQUENCE_SEVERAL`: telling an ordinary
# submitter his note's decisions cannot be linked describes something that does not exist, and
# instructions a reader knows are not about him are instructions he stops reading.
ONE_ASK_CLAUSE_SEVERAL = (
    "This is the only question this capture gets, for all {n} at once: if even one of them is "
    "still unplaced after your reply, the whole {noun} parks for a steward.")

MEETING_CONSEQUENCE_SEVERAL = (
    " Not just the decision that names it — a meeting page can never link a decision that was "
    "never filed.")


def _parked_noun(meeting: bool) -> str:
    """What the submitter's own material IS, in his words. A transcript became a whole page set,
    and "capture" would hide from him how much is stuck behind one unplaced name."""
    return "meeting" if meeting else "capture"


def _numbered_names(names: list[str]) -> str:
    return "\n".join(f'  {i}. "{_clean(name, 120)}"' for i, name in enumerate(names, start=1))


def _named_only(values, clean) -> list[str]:
    """The values that are ACTUALLY a name, normalised. Surrounding whitespace is not part of a
    name: the same string is quoted back at the submitter AND offered to a steward as
    `birth.prepare --name`, where `" Jack "` and `"Jack"` mint two registry entities that will
    never match each other. `entities.birth._prepare` refuses a whitespace-only name outright,
    so a blank is not a name here either — `_clean` alone cannot see this, because `sanitize`
    and `clamp` never strip."""
    return [name for name in (clean(v, 120).strip() for v in _as_list(values)) if name]


def _one_name_question(*, submission_id, name: str, lines: list[str], total: int) -> str:
    """The question a capture with exactly ONE unresolved name gets. Kept as its own sentence
    rather than folded into the plural wording: "your material names 1 things" is how a reader
    learns nobody read what he was sent. The SHAPE of the report is the same either way — this
    decides only the prose."""
    head = (f"{schema.NEEDS_INPUT} — capture #{submission_id} is parked on one question before it "
            f"can be filed: your material seems to be about \"{name}\", and the entity "
            f"registry doesn't recognize that name.")
    if not total:
        return (f"{schema.NEEDS_INPUT} — capture #{submission_id} is parked on one question before "
                f"it can be filed: your material seems to be about \"{name}\", and nothing is "
                f"registered in the entity registry yet — there is nothing to match it against.\n\n"
                f"Reply saying it's new (or, if you think it should already be registered, say what "
                f"you expected) — a steward takes it from there either way; your material stays "
                f"archived until they do. This is the only question this capture gets.")
    if lines:
        return (f"{head} Here is everything registered today:\n\n"
                + "\n".join(lines)
                + "\n\nReply naming one of these exactly if your material is about it. If it's new, "
                  "or you're not sure, say so — a steward takes it from there; your material stays "
                  f"archived either way. {ONE_ASK_CLAUSE}")
    return (f"{head} The registry has {total} entities registered today — too many to list "
            f"here.\n\nAnswer with the exact name of whatever your material is actually about "
            f"and we'll match it, aliases included. If it's new, or you're not sure, say so — a "
            f"steward takes it from there; your material stays archived either way. "
            f"{ONE_ASK_CLAUSE}")


def _several_names_question(*, submission_id, names: list[str], lines: list[str], total: int,
                            clause: str) -> str:
    """The question a capture with MORE THAN ONE unresolved name gets: the names listed numbered
    and UNCAPPED, because every one of them is something the reply is REQUIRED to place."""
    n = len(names)
    head = (f"{schema.NEEDS_INPUT} — capture #{submission_id} is parked on one question before it "
            f"can be filed: your material names {n} things the entity registry doesn't "
            f"recognize:\n\n{_numbered_names(names)}\n")
    if not total:
        return (f"{head}\nNothing is registered in the entity registry yet, so there is nothing "
                f"to match any of them against.\n\nReply saying, for each of the {n}, that it's "
                f"new (or, if you think one should already be registered, say what you expected) "
                f"— a steward takes it from there either way; your material stays archived until "
                f"they do. {clause}")
    if lines:
        return (f"{head}\nHere is everything registered today:\n\n" + "\n".join(lines)
                + f"\n\nReply once, covering all {n}: for each name above, say which registered "
                  f"entity it is, that it's new, or that you're not sure — a steward takes over any "
                  f"you can't place; your material stays archived either way. {clause}")
    return (f"{head}\nThe registry has {total} entities registered today — too many to list "
            f"here.\n\nAnswer with the exact name of whatever each of the {n} is actually "
            f"about and we'll match it, aliases included; for any that's new, or you're not "
            f"sure, say so — a steward takes it from there. {clause}")


def needs_input(*, submission_id, names: list[str], candidates=(),
                total_candidates: int | None = None, agent_rationale: str = "",
                findings: list = (), meeting: bool = False) -> dict:
    """The librarian's one question, CODE-BUILT rather than agent prose, for ANY number of
    unresolved names — there is no singular sibling and no singular key.

    Three candidate shapes: a registry under `MAX_QUESTION_CANDIDATES` is shown whole, an empty one
    says so, one too large names the count and lists NOTHING. For a human, never a repair brief
    (`gates.anchoring_brief` is that). The invocation is on its own line AND travels as
    `reply_invocation`, because a reader's LLM paraphrase could compress the command away and leave
    the promise false of what he saw. `meeting` selects the flow's own noun and consequence for the
    several-names clause; it changes no structure, only what the sentence claims is at stake.

    `unresolved_names` is ALWAYS the written key, a list even for one name. The retired
    `unresolved_name` is read-only legacy — see `capture.schema.SITUATION_NAME_KEY`.
    """
    names = _named_only(names, _clean) or [schema.UNNAMED_ENTITY_PLACEHOLDER]
    n = len(names)
    invocation = schema.reply_invocation(submission_id)
    lines = _candidate_lines(candidates)
    total = len(_as_list(candidates)) if total_candidates is None else int(total_candidates)

    if n == 1:
        body = _one_name_question(submission_id=submission_id, name=names[0], lines=lines,
                                  total=total)
        reply_line = invocation
        question = f"which entity is {names[0]}?"
    else:
        clause = (ONE_ASK_CLAUSE_SEVERAL.format(n=n, noun=_parked_noun(meeting))
                  + (MEETING_CONSEQUENCE_SEVERAL if meeting else ""))
        body = _several_names_question(submission_id=submission_id, names=names, lines=lines,
                                       total=total, clause=clause)
        reply_line = invocation.replace('<your answer>', f'<your answer, covering all {n}>')
        question = f"which entities are {', '.join(names)}?"

    summary = f"{body}\n\nReply with:\n  {reply_line}"
    return base_report(status=schema.NEEDS_INPUT, summary=summary,
                       agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
                       findings=list(_as_list(findings)),
                       open_question=question,
                       # The command as a FACT beside the sentence stating it.
                       reply_invocation=invocation, unresolved_names=names)


# ── triage ────────────────────────────────────────────────────────────────────────────────────
def triage_entity(*, names: list[str], agent_rationale: str = "", findings: list = (),
                  asked: bool = False, meeting: bool = False) -> dict:
    """Parked because the thing it is about is not a registered entity — the triage flavor that may
    still resolve, so it must not collapse into `triage_type`'s string. `asked` is the one thing it
    distinguishes, and must, or a person who just replied is told no follow-up exists.

    ONE builder for any number of names, writing `schema.SITUATION_NAMES_KEY` — a list even for one
    name — and never the singular key: steward tooling reads it per name, so approving one is never
    blocked by another. `meeting` only names the parked thing as its submitter knows it (see
    `_parked_noun`), which the one-name sentence has no slot for.
    """
    # `_clean_identity`: an entity NAME is a field a steward is invited to paste into a shell. The
    # no-name fallback is the SHARED constant, never a second copy of the words: the key below
    # carries it onto the parked row, and the two surfaces that refuse it by value
    # (`entities.cli._suggestable`, `entities.situations.mint_name_prefill`) compare against that
    # same constant — a local literal here silently unrefuses it.
    clean_names = _named_only(names, _clean_identity) or [schema.UNNAMED_ENTITY_PLACEHOLDER]
    n = len(clean_names)
    tail = ("You already answered the one question this capture gets, and the answer still "
            "doesn't match a registered entity — so a steward takes it from here and you won't be "
            "asked again. Nothing further is needed from you; your material stays archived until "
            "it's reviewed." if asked else
            "Nothing further is needed from you — no question is coming about this one; your "
            "material stays archived until it's reviewed.")
    if n == 1:
        # One name reads as one name — see `_one_name_question` for why the plural wording is not
        # stretched over it.
        name = clean_names[0]
        summary = (f"{schema.TRIAGE} — parked, not filed. Your material seems to be about "
                   f"\"{name}\", which the entity registry doesn't recognize yet, so it can't "
                   f"be anchored. A steward will register {name} as a new entity or place "
                   f"this where it actually belongs. {tail}")
        question = f"which entity is {name}?"
    else:
        quoted = [f'"{name}"' for name in clean_names]
        listed = ", ".join(quoted[:-1]) + f" and {quoted[-1]}"
        summary = (f"{schema.TRIAGE} — parked, not filed. Your material named {n} things the entity "
                   f"registry doesn't recognize — {listed} — and at least one of them still "
                   f"doesn't match a registered entity. A steward will register whichever of these "
                   f"are new, or place this {_parked_noun(meeting)} where it actually belongs. "
                   f"{tail}")
        question = f"which entities are {', '.join(clean_names)}?"
    return base_report(status=schema.TRIAGE, summary=summary,
                       agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
                       findings=list(_as_list(findings)),
                       asked=bool(asked),
                       # `stigmergy-entities` selects approvable rows on this key and pre-fills
                       # `--name` from here rather than by parsing `open_question`.
                       **{schema.SITUATION_KEY: schema.SITUATION_UNRESOLVED_ENTITY,
                          schema.SITUATION_NAMES_KEY: clean_names},
                       open_question=question)


def triage_type(*, judged_type: str, agent_rationale: str = "", findings: list = ()) -> dict:
    """Parked because the fast lane does not file that type — the triage flavor that, as submitted,
    never resolves. The label carries its own article and is dropped in whole; an UNKNOWN type
    cannot be articled, so carrying `material` is the fallback's job, not every label's."""
    # The same fallback word `processing._triage` uses, so a missing field renders one way.
    clean_type = _clean(judged_type, 60) or "unknown"
    # Label, list and count come from `page.PAGE_TYPES`, so the table's count cannot go stale here.
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
                       # Reaches `stigmergy-entities` too: `person`/`team`/`product` material is an
                       # identity situation a steward may answer by minting the entity.
                       **{schema.SITUATION_KEY: schema.SITUATION_UNSUPPORTED_TYPE,
                          schema.SITUATION_TYPE_KEY: clean_type},
                       open_question=f"where does {label} belong?")


# ── failed ────────────────────────────────────────────────────────────────────────────────────
def failed_system(*, attempts: int, stage: str, reason: str, agent_attempts: int = 0,
                  cost_usd: float = 0.0, findings: list = ()) -> dict:
    """The librarian could not comply — a different sentence SHAPE from every `rejected` above,
    since nothing is the submitter's to fix and resubmitting may well work. BOTH counters are
    named (queue deliveries, agent attempts inside the last) so an operator sees the retry."""
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
    """The one sentence an injection attempt produces: a CATEGORY from the fixed set, never a
    substring of the planted instruction — that would deliver a second copy to a human."""
    return (f"finding: material attempted to instruct the librarian directly "
            f"(category: {category}) — not followed; filed as ordinary content only.")


# ── the second renderer: prose for a terminal ─────────────────────────────────────────────────
def render_prose(report: dict) -> str:
    """The CLI's rendering of the same fact set; the summary sentence is reused verbatim.
    `agent_rationale` renders unconditionally on the FILED path but only when present on the parked
    paths, which carry no field block."""
    lines = [report.get("summary", "")]
    status = report.get("status")
    is_meeting = "filed_meeting" in report
    if status == schema.FILED and not is_meeting:
        # `filed_meeting` renders its own field block INTO `summary`; appending these duplicates.
        lines.append(f"  links_created    {_listed(report.get('links_created'))}")
        lines.append(f"  overlaps_flagged {_listed(report.get('overlaps_flagged'))}")
        lines.append(f"  pages_edited     {_listed(report.get('pages_edited'))}")
        lines.append(f"  agent_rationale  {report.get('agent_rationale') or NONE_LABEL}")
    elif status == schema.FILED:
        # The meeting case, explicit rather than a fallthrough: nothing is left to append.
        pass
    elif report.get("agent_rationale"):
        lines.append(f"  agent_rationale  {report['agent_rationale']}")
    for finding in report.get("findings", []):
        lines.append(f"  ! {finding}")
    return "\n".join(lines)
