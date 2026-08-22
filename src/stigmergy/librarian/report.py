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

NONE_LABEL = "(none)"

# Wider than the 200-character clamp the decorative fields get: this field's CONTENT is the point.
RATIONALE_WIDTH = 400

# Re-exported from `capture.schema`: `capture` may not import `librarian`, so the vocabulary two
# packages must agree on sits with the column that stores it.
SEARCHABILITY_NOTE = schema.SEARCHABILITY_NOTE
base_report = schema.base_report

def _clean(text: str, width: int = 0) -> str:
    """Untrusted text on its way to a human — `stigmergy.text.clamp` + `sanitize`, the same seam
    `capture.render.clean_for_terminal` uses, so the two packages' renderers cannot disagree
    about truncation."""
    return textutil.clamp(textutil.sanitize(str(text or "")).replace("\n", " "), width)


# An IDENTITY field is not prose: a newborn entity's name and id come off CAPTURED MATERIAL and
# land in a report a person reads in a terminal, pastes into a shell and searches the repo by —
# and `sanitize` strips control characters only. The set below is what would change the meaning of
# any of those, so it is stripped whether or not a command happens to exist today.
_UNSAFE_IN_IDENTITY = set('/\\:*?"<>|[]#^') | set("'`$;&(){}!~\n\r\t")


def _clean_identity(text: str, width: int = 0) -> str:
    """`_clean`, then shell/filename metacharacters stripped; for NAMES, never prose. Stripped
    rather than refused: a capture must always be reportable, or a hostile name becomes a lost row."""
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
# What an ENTITY anchor's own `reason` is introduced by. Entity resolution is the agent's judgment
# now (`kernel.normalize`: code folds accents and punctuation, never a legal form or a former
# name), and an automatic decision nobody can see is exactly what this repo does not allow — so
# whenever the agent says WHY a capture points where it points, the person who submitted it reads
# that sentence beside the anchor. Company-wide scope keeps carrying its reason inside
# `_anchor_phrase` instead: there the reason justifies belonging to NO entity, and it is required
# by `gate_anchoring` rather than volunteered.
RESOLUTION_PREFIX = "Resolved:"


def _resolution_note(anchoring: dict, registry=None) -> str:
    """The agent's stated reason for an ENTITY anchor, cleaned — `""` when there is none to state.

    Only for `kind == "entity"`: a company-wide reason is already inside the anchor phrase, and
    printing it twice would read as two different facts. Never a claim code can check — the whole
    point is that the JUDGMENT is the agent's and the FENCE is code's (the id must resolve in the
    registry, or `gate_anchoring` refuses the filing outright).
    """
    anchoring = anchoring if isinstance(anchoring, dict) else {}
    if str(anchoring.get("kind", "")).lower() != "entity":
        return ""
    # `registry` is UNUSED and stays in the signature for symmetry with `_anchor_phrase`: the two
    # are called as a pair at both call sites, from the same two facts, and a caller having to
    # remember which of them takes the registry is how one of them gets the wrong one. Registry-
    # resolvable or not, the sentence is the same — `_anchor_phrase` already says which ids the
    # page carries, and this says why.
    del registry
    return _clean(anchoring.get("reason", ""), RATIONALE_WIDTH)


def filed(*, page_path: str, commit: str, anchoring: dict, links: list, overlaps: list,
          findings: list, pages_edited: list = (), agent_rationale: str = "", registry=None,
          source_pages: list = (), entities_born: list = (), aliases_added: list = (),
          entities_updated: list = ()) -> dict:
    """The ordinary success: page, commit and anchor, plus the not-searchable-yet clause in the same
    sentence. `pages_edited` is what code actually wrote from the agent's declared edits, while
    `overlaps_flagged` is the agent's JUDGMENT about which pages overlap — a different field.

    An entity anchor's `reason` rides beside the anchor as `anchor_reason` and in the sentence,
    never folded into `anchored_to`: that field names the identity a read path branches on, and a
    rationale glued onto it would make one field two facts.

    `entities_born` / `aliases_added` are the identities this filing CREATED
    (`librarian.identity`): the submitter is told the page landed AND which identities their own
    capture introduced, because "filed" alone would hide that the registry grew.
    """
    anchor = _anchor_phrase(anchoring, registry)
    resolution = _resolution_note(anchoring, registry)
    resolved_clause = f" {RESOLUTION_PREFIX} {resolution}" if resolution else ""
    summary = (f"{schema.FILED} — {_clean(page_path)}@{commit}, anchored to {anchor}."
               f"{resolved_clause} {SEARCHABILITY_NOTE}")
    overlap_paths = [_path_of(o) for o in _as_list(overlaps)]
    if overlap_paths:
        # Visibly different from the exact-duplicate refusal: this one FILED.
        others = _listed(overlap_paths)
        summary = (f"{schema.FILED} — {_clean(page_path)}@{commit}, anchored to {anchor}."
                   f"{resolved_clause} "
                   f"This overlaps existing material at {others}; both pages now cross-link and "
                   f"carry an overlap note — nothing was deleted or rewritten on either side. "
                   f"{SEARCHABILITY_NOTE}")
    source_paths = [_clean(path, 200) for path in _as_list(source_pages)]
    if source_paths:
        summary += (f" The captured material itself is filed verbatim at "
                    f"{_listed(source_paths)}, and the page cites it in `sources:`.")
    born, added_aliases = _birth_lists(entities_born, aliases_added)
    updated = _updated_list(entities_updated)
    summary += births_clause(born, added_aliases, updated)
    return base_report(
        status=schema.FILED, summary=summary,
        page_path=page_path, commit=commit, anchored_to=anchor,
        anchor_reason=resolution,
        links_created=[_clean(link, 120) for link in _as_list(links)],
        overlaps_flagged=[_clean(path, 200) for path in overlap_paths],
        pages_edited=[_clean(path, 200) for path in _as_list(pages_edited)],
        agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
        findings=list(_as_list(findings)),
        entities_born=born, aliases_added=added_aliases,
        entities_updated=updated,
        **({"source_pages": source_paths} if source_paths else {}))


# ── births: the identities a filing created ───────────────────────────────────────────────────
def _updated_list(entities_updated) -> list:
    return [{"entity": _clean_identity((u or {}).get("entity", ""), 80),
             "facts": int((u or {}).get("facts") or 0),
             "connections": int((u or {}).get("connections") or 0)}
            for u in _as_list(entities_updated) if isinstance(u, dict)]


def _birth_lists(entities_born, aliases_added) -> tuple[list, list]:
    """The two lists as the report carries them — cleaned, identity fields through the identity
    cleaner, never a raw object from the worker."""
    born = []
    for e in _as_list(entities_born):
        if not isinstance(e, dict):
            continue
        entry = {"id": _clean_identity(e.get("id", ""), 80),
                 "name": _clean_identity(e.get("name", ""), 120),
                 "type": _clean(e.get("type", ""), 40)}
        # Who the identity is confirmed by — the capture's own submitter, on every entity (ADR 044).
        if e.get("confirmed_by"):
            entry["confirmed_by"] = _clean_identity(e["confirmed_by"], 120)
        born.append(entry)
    aliases = [{"entity": _clean_identity((a or {}).get("entity", ""), 80),
                "alias": _clean_identity((a or {}).get("alias", ""), 120)}
               for a in _as_list(aliases_added) if isinstance(a, dict)]
    return born, aliases


def births_clause(born: list, added_aliases: list, updated: list = ()) -> str:
    """The sentence telling a submitter the page landed AND which identities their capture
    introduced. Empty when nothing was created, so an ordinary filing's sentence is unchanged."""
    parts = []
    if born:
        named = _listed([f"{e['name']} (`{e['id']}`)" for e in born])
        parts.append(f"It introduces {_plural(len(born), 'new entity', 'new entities')}: "
                     f"{named} — the page is written from the material and what the brain held, "
                     f"and the identity is confirmed by you.")
    if added_aliases:
        named = _listed([f"\"{a['alias']}\" for `{a['entity']}`" for a in added_aliases])
        parts.append(f"It teaches the registry {_plural(len(added_aliases), 'new spelling')}: "
                     f"{named} — resolving from now on.")
    for u in updated:
        added = [w for w in (_plural(int(u.get("facts") or 0), "fact") if u.get("facts") else "",
                             _plural(int(u.get("connections") or 0), "connection")
                             if u.get("connections") else "") if w]
        if added:
            parts.append(f"It adds {' and '.join(added)} to the page of `{u.get('entity', '')}`.")
    return (" " + " ".join(parts)) if parts else ""


# ── filed_meeting: the report for a page SET ──────────────────────────────────────────────────
def filed_meeting(*, source_pages: list, meeting_page: str, decisions: list, commit: str,
                  pages_edited: list = (), agent_rationale: str = "", registry=None,
                  entities_born: list = (), aliases_added: list = (),
                  entities_updated: list = ()) -> dict:
    """`filed`'s sibling for a page SET: N >= 1 source pages, a meeting page, and N decision pages,
    each with its OWN anchor outcome. `decisions` is `[{"path": ..., "anchoring": ...}]`, and
    `result_ref` names the MEETING PAGE alone or `dedup.Match.page_path`'s `rsplit("@")` breaks.

    `pages_edited` is `filed`'s own field with `filed`'s own meaning — what code actually wrote from
    the agent's declared edits, on pages this capture did NOT create. It was a hardcoded `(none)`
    line for as long as this flow had no edit mechanism; a page a commit changes and no report names
    is a page nobody knows was touched."""
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
            anchoring = d.get("anchoring") or {}
            anchor = _anchor_phrase(anchoring, registry)
            # Per DECISION, because a meeting anchors each one independently: one resolution
            # judgment per page, and the reader has to be able to tell which page it was about.
            resolution = _resolution_note(anchoring, registry)
            lines.append(f"    {index}. {_clean(d.get('path', ''))} — anchored to {anchor}"
                         + (f". {RESOLUTION_PREFIX} {resolution}" if resolution else ""))
            decision_rows.append({"path": _clean(d.get("path", ""), 200), "anchored_to": anchor,
                                  "anchor_reason": resolution})
    else:
        lines.append("  decision pages    (none — nothing from this meeting was drafted as a "
                     "decision worth its own page)")
    edited_paths = [_clean(path, 200) for path in _as_list(pages_edited)]
    lines.append(f"  links_created     {NONE_LABEL}")
    lines.append(f"  overlaps_flagged  {NONE_LABEL}")
    lines.append(f"  pages_edited      {_listed(edited_paths)}")
    lines.append(f"  agent_rationale   {_clean(agent_rationale, RATIONALE_WIDTH) or NONE_LABEL}")
    born, added_aliases = _birth_lists(entities_born, aliases_added)
    updated = _updated_list(entities_updated)
    if born or added_aliases or updated:
        lines.append("")
        lines.append("  " + births_clause(born, added_aliases, updated).strip())
    return base_report(
        status=schema.FILED, summary="\n".join(lines), page_path=meeting_page, commit=commit,
        agent_rationale=_clean(agent_rationale, RATIONALE_WIDTH),
        pages_edited=edited_paths,
        # The structured sibling of the rendered lines above, for a caller that branches on facts.
        filed_meeting={"source_pages": [_clean(p, 200) for p in source_pages],
                      "meeting_page": meeting_page, "decisions": decision_rows},
        entities_born=born, aliases_added=added_aliases, entities_updated=updated)


def filed_retry(*, original_id: int, page_path: str, commit: str) -> dict:
    """Retry collapse. Reads as neither a fresh success nor a penalty: the material IS filed, at
    that page, and this row is the same capture arriving twice."""
    summary = (f"{schema.FILED} — same content as submission #{original_id}, already committed "
               f"as {_clean(page_path)}@{commit}; this row is a retry of that one, not a second "
               f"capture, so nothing new was written.")
    return base_report(status=schema.FILED, summary=summary, page_path=page_path, commit=commit,
                       retry_of=original_id)


def filed_delete(*, deleted: list, rewritten: dict, commit: str, model_calls: int = 0) -> dict:
    """A removal that landed. The one report that carries page BYTES: nobody read the prose the
    sweep wrote before it was pushed (ADR 043 D5), so the per-page diff travels in the row and the
    read surfaces show it — ACL-scoped and fenced by whoever renders it, exactly as
    `brain_delete`'s own response used to be.

    `deleted` is the pages that stopped existing and `rewritten` is `{path: unified diff}` for the
    pages that no longer point at them. Both are needed: a reader who saw only the diffs would not
    know what went, and one who saw only the paths would not know what a model wrote in their name.
    """
    n_gone, n_rewritten = len(deleted or ()), len(rewritten or {})
    summary = (
        f"{schema.FILED} — removed {n_gone} {_plural(n_gone, 'page')} and rewrote {n_rewritten} "
        f"that referred to them, as commit {commit[:12]}. Nobody read the rewritten prose before "
        f"it landed: the diffs on this row are that reading, and `git revert` in the knowledge "
        f"repo is the undo.")
    return base_report(status=schema.FILED, summary=summary, commit=commit,
                       deleted=[_clean(p) for p in (deleted or ())],
                       rewritten={_clean(path): text for path, text in (rewritten or {}).items()},
                       model_calls=int(model_calls))


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
    return _rejected(schema.REASON_DUPLICATE, summary, page_path=page_path)


def rejected_unremovable(*, reason: str) -> dict:
    """A removal the worker could not perform. `reason` is the deletion lane's own sentence — it
    names repo-relative paths and what it could not do, and it is written to be published, which is
    what lets it travel verbatim into a report the person who asked reads back."""
    summary = (f"{schema.REJECTED} — this removal was not performed: {_clean(reason, 600)} "
               f"Nothing was deleted and nothing was committed.")
    return _rejected(schema.REASON_UNREMOVABLE, summary)


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
    return _rejected(schema.REASON_STEERING, summary, findings=list(findings))


def rejected_malformed_frontmatter(*, findings: list = ()) -> dict:
    """The stamped page's frontmatter is not valid YAML — content-caused, so routed here only when
    that finding is the WHOLE veto and the fix is the submitter's."""
    summary = (f"{schema.REJECTED} — the frontmatter in your material could not be turned into a "
               f"valid page; nothing was filed and no partial page exists. This usually means a "
               f"list-shaped field (`entity:`, `acl:`, `related:`, ...) was written across "
               f"multiple lines without the continuation indented under its key. Resubmit with "
               f"that field as a single-line list, e.g. `entity: [\"acme\"]`, or with its "
               f"continuation lines indented under the key.")
    return _rejected(schema.REASON_MALFORMED_FRONTMATTER, summary, findings=list(findings))


def rejected_forged_field(*, findings: list = ()) -> dict:
    """`gate_frontmatter`'s `forged-field` and `forbidden-field` codes. Carries the sibling's
    `REASON_MALFORMED_FRONTMATTER` deliberately: a read path branches on the code, one cause."""
    summary = (f"{schema.REJECTED} — your material declared a frontmatter field it may not "
               f"assert: either one the server computes itself (`owner`, `acl`, `entity`, "
               f"`content_hash`, `id`) or `verification`, which nothing computes since the trust "
               f"layer was removed and which therefore no page may claim. Nothing was filed and "
               f"no partial page exists. Remove that field from what you submit and resubmit — "
               f"the librarian fills in what it computes.")
    return _rejected(schema.REASON_MALFORMED_FRONTMATTER, summary, findings=list(findings))


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
               f"capture. Nothing further happens automatically: an operator needs to look at "
               f"the fault. Resubmitting may work, since the librarian is not deterministic, but it "
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
    `agent_rationale` renders unconditionally on the FILED path but only when present on the refused
    paths, which carry no field block."""
    lines = [report.get("summary", "")]
    status = report.get("status")
    is_meeting = "filed_meeting" in report
    if status == schema.FILED and not is_meeting:
        # `filed_meeting` renders its own field block INTO `summary`; appending these duplicates.
        lines.append(f"  anchor_reason    {report.get('anchor_reason') or NONE_LABEL}")
        lines.append(f"  links_created    {_listed(report.get('links_created'))}")
        lines.append(f"  overlaps_flagged {_listed(report.get('overlaps_flagged'))}")
        lines.append(f"  pages_edited     {_listed(report.get('pages_edited'))}")
        lines.append(f"  agent_rationale  {report.get('agent_rationale') or NONE_LABEL}")
        born = report.get("entities_born") or []
        if born:
            lines.append("  entities_born "
                         + _listed([f"{e.get('name')} ({e.get('id')})" for e in born]))
        if report.get("aliases_added"):
            lines.append("  aliases_added "
                         + _listed([f"{a.get('alias')} -> {a.get('entity')}"
                                    for a in report["aliases_added"]]))
    elif status == schema.FILED:
        # The meeting case, explicit rather than a fallthrough: nothing is left to append.
        pass
    elif report.get("agent_rationale"):
        lines.append(f"  agent_rationale  {report['agent_rationale']}")
    for finding in report.get("findings", []):
        lines.append(f"  ! {finding}")
    return "\n".join(lines)
