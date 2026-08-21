// The console's VOCABULARY — every system word the API sends, with the human label, the one-line
// meaning and WHO is acting when it applies. Copy lives here and nowhere else, so a word means
// the same thing on every page. The closed lists themselves (statuses, kinds, severities…) come
// from /admin/api/meta; this file only knows how to SAY them, and falls back to the raw word for
// anything it has not met yet — a new status renders ugly, never invisible.

// ── the key: colour is who decides ───────────────────────────────────────────────────────────
// The README's own convention, carried into every status, chart and timeline here.
export const KEY = {
  human: { label: "a human",
    explain: "a person is waited on, or a person decided — stewards, submitters, you" },
  model: { label: "the model",
    explain: "an agent drafted, gathered or proposed; it is never the last word on anything" },
  code: { label: "code",
    explain: "deterministic gates and rules decided — a refusal, a lease, a schedule" },
  git: { label: "git",
    explain: "it landed in the knowledge repo: a commit, the one thing here that is not rebuildable" },
  fail: { label: "broke",
    explain: "something could not finish — the librarian, a job, a push" },
};

// Every other closed word the console renders as a pill — verdicts in the ledger, proposal and
// job outcomes, GitHub's workflow states — with its human label and who decided it. Keyed on the
// raw word; an unknown word renders as itself.
export const WORD = {
  approve: { label: "approved", who: "git" }, approved: { label: "approved", who: "model" },
  reject: { label: "declined", who: "code" }, rejected: { label: "declined", who: "code" },
  request_changes: { label: "changes requested", who: "human" },
  merge: { label: "merged", who: "git" },
  // legacy verdicts, on ledger rows from before captures stopped parking
  requeue: { label: "requeued", who: "model" }, resolve: { label: "resolved by hand", who: "human" },
  applied: { label: "applied", who: "git" }, pending: { label: "waiting", who: "human" },
  failed: { label: "failed", who: "fail" }, failure: { label: "failed", who: "fail" },
  ok: { label: "ok", who: "git" }, success: { label: "succeeded", who: "git" },
  error: { label: "error", who: "fail" }, partial: { label: "partial", who: "human" },
  cancelled: { label: "cancelled", who: "code" }, in_progress: { label: "running", who: "model" },
  queued: { label: "queued", who: "code" }, refused: { label: "refused", who: "code" },
  built: { label: "built", who: "git" },
  active: { label: "scheduled", who: "git" },
  disabled_manually: { label: "paused by a person", who: "human" },
  disabled_inactivity: { label: "auto-paused by GitHub (60 days of no repository activity) — press Enable", who: "human" },
};

export function word(raw) {
  return WORD[raw] || { label: String(raw).replaceAll("_", " "), who: "code" };
}

// A decision as a past-tense verb in a sentence ("marc declined identity decision #41").
const DECISION_VERB = {
  approve: "approved", reject: "declined", merge: "merged", request_changes: "asked for changes to",
  requeue: "requeued", resolve: "resolved by hand",
};

export function decisionVerb(verdict) {
  return DECISION_VERB[verdict] || `${verdict}d`.replaceAll("_", " ");
}

// ── capture statuses (the queue's own machine) ───────────────────────────────────────────────
export const STATUS = {
  queued: { label: "Waiting for the librarian", short: "queued", who: "code",
    explain: "archived and attributed; the librarian claims it next" },
  claimed: { label: "Being filed now", short: "filing", who: "model",
    explain: "a worker holds the lease; the agent is drafting a page" },
  filed: { label: "Landed in git", short: "filed", who: "git",
    explain: "a page exists; the nine gates approved exactly this diff — and any entity it proposed exists too, waiting on a steward" },
  resolved: { label: "Handled by hand", short: "resolved", who: "human",
    explain: "a steward closed it by hand, back when captures could park — nothing writes this any more" },
  rejected: { label: "Declined", short: "declined", who: "code",
    explain: "a gate refused it, or a steward declined it; the reason reached the submitter" },
  failed: { label: "Could not finish", short: "failed", who: "fail",
    explain: "the librarian ran out of attempts; an ingest error records the stage" },
};

export function status(word) {
  return STATUS[word] || { label: word, short: word, who: "code", explain: "" };
}

// Which statuses a chart stacks, in the order that keeps red and green apart (CVD).
export const OUTCOME_ORDER = ["filed", "resolved", "queued", "claimed", "rejected", "failed"];

// ── review-queue item kinds ──────────────────────────────────────────────────────────────────
export const ITEM_KIND = {
  "identity-proposal": { label: "Proposed entity", who: "model",
    explain: "the librarian met a name the registry did not know and created the entity page itself, unconfirmed — approve it, merge it into the entity it really is, or decline it" },
  "alias-proposal": { label: "Proposed spelling", who: "model",
    explain: "a name the material used for a registered entity, which the registry did not list — approve it as one of its names, or decline it" },
  "repair-proposal": { label: "Repair proposal", who: "model",
    explain: "the nightly proposer read the gardener's findings and drafted a fix; approving "
      + "applies exactly that as one commit" },
};

export function itemKind(raw) {
  return ITEM_KIND[raw] || { label: raw, who: "human", explain: "" };
}

// ── repair kinds ─────────────────────────────────────────────────────────────────────────────
export const REPAIR_KIND = {
  edits: { label: "Additive edits",
    explain: "a link added to a page's related list, and for overlap or contradiction a "
      + "one-sentence callout — nothing is rewritten or deleted" },
  "entity-body": { label: "Drafted entity body",
    explain: "replaces the page's body below its title with a draft the model wrote; reading "
      + "the draft IS the review" },
  delete: { label: "Page removal",
    explain: "pages stop existing, and every page that linked to them is rewritten so the "
      + "link is gone — undoing it means a revert in the knowledge repo" },
  "entity-alias": { label: "Entity merge",
    explain: "two registry entries were one entity: the survivor absorbs the other's "
      + "spellings, the absorbed page is marked superseded, anchored pages move" },
};

export function repairKind(word) {
  return REPAIR_KIND[word] || { label: word || "edits", explain: "" };
}

// ── the registry check: the birth gate's own verdict on a name ──────────────────────────────
export const VERDICT = {
  registered: { label: "Already registered", tone: "git",
    explain: "this spelling already resolves to a registered entity — a proposal with this verdict "
      + "is that entity under another name: merge it." },
  collides: { label: "Would collide", tone: "fail",
    explain: "the birth gate refuses this name: it would be confused with an entity that already "
      + "exists. If it is the same thing, merge (or add the spelling as an alias); if it is "
      + "different, it needs a name that cannot be confused." },
  similar: { label: "Looks similar", tone: "human",
    explain: "nothing blocks it, but these registered entities share a distinctive word — "
      + "check they are not the same thing under another name before confirming a second one." },
  clear: { label: "Nothing like it registered", tone: "accent",
    explain: "no registered entity resolves to it or would be confused with it — the gate still "
      + "checks again against the repo as it stands when the commit lands" },
  unchecked: { label: "Could not check", tone: "code",
    explain: "this server has no readable registry to check against; the birth gate still runs" },
};

export function verdict(word) {
  return VERDICT[word] || { label: word, tone: "code", explain: "" };
}

// ── the gardener's checks ────────────────────────────────────────────────────────────────────
export const CHECK = {
  "orphan-page": "a page nothing links to and that links to nothing",
  "aging-seed": "a page still marked seed long after it was filed",
  "stale-view": "an entity view older than filings anchored to it",
  "anchor-concentration": "too many recent filings anchored to one entity",
  "dead-vocabulary": "a registered entity no page cites",
  "company-wide-fraction": "too large a share of recent pages declare company-wide scope",
  "company-page-names-entity": "a company-wide page names an entity in its body",
  "date-bearing-body-link": "prose links a dated source page instead of the decision",
  "entity-placeholder-body": "an entity page whose body is still the template",
  "anchored-to-superseded-entity": "a page anchored to an entity that was merged away",
  "model-contradiction": "two pages state incompatible facts (model sweep)",
  "model-anchor-fit": "a page anchored to an entity it is not really about (model sweep)",
  "model-unlinked-mention": "a page names an entity without linking it (model sweep)",
  "model-superseded-canon": "a superseded page still reads as the canonical one (model sweep)",
  "model-empty-entity-body": "an entity page whose body says nothing about it (model judged)",
  "model-duplicate-entity": "two registered entities that may be one (model judged)",
};

export function check(slug) {
  return CHECK[slug] || "";
}

export const SEVERITY = {
  sla: { label: "urgent", explain: "triggers the Slack notice — nothing produces one yet" },
  warn: { label: "warning", explain: "worth a steward's look this week" },
  info: { label: "note", explain: "good to know; nothing is wrong" },
  error: { label: "error", explain: "the substrate is inconsistent — fix before trusting answers" },
};

export function severity(word) {
  return SEVERITY[word] || { label: word, explain: "" };
}

// ── the four workflows ───────────────────────────────────────────────────────────────────────
// `consequence` is what the Run-now confirmation says — what happens, in the reader's terms,
// before the irreversible ones. `retentionDays` is spliced in from `meta().retention`.
export const JOB = {
  "index-rebuild.yml": {
    purpose: "rebuilds the whole search index from the knowledge repo — real embedder, real spend",
    truth: "the index's own built_at; a rebuild writes no job row",
    consequence: "runs a FULL index rebuild in GitHub Actions — real embedder, real spend, against this database. Answers keep being served from the old index until it lands.",
  },
  "retention-purge.yml": {
    purpose: "strips payload and hints from captures that have been terminal for a while",
    truth: "the latest capture-purge job row",
    consequence: "strips the payload and the placement hints from every capture that has been terminal for {days} days — permanently, in GitHub Actions, against this database. Id, submitter, timestamps, state and result pointer survive; evidence blobs are untouched. Leave Dry run ticked to list what it would take without touching anything (Captures → Retention purge previews the exact rows).",
  },
  "gardener.yml": {
    purpose: "runs the deterministic corpus-health checks and the model passes; findings persist "
      + "and the Repairs proposer reads them next morning",
    truth: "the latest gardener job row",
    consequence: "runs the deterministic checks AND the model passes in GitHub Actions — real model spend; findings persist to this database and feed tomorrow's repair proposals.",
  },
  "repair-propose.yml": {
    purpose: "reads the gardener's findings and drafts repair proposals — it applies nothing",
    truth: "the latest repair-propose job row",
    consequence: "reads the latest gardener findings and proposes repairs in GitHub Actions — real model spend. It applies nothing: every proposal lands pending, for the Repairs page.",
  },
};

export function jobConsequence(file, title, retentionDays) {
  const copy = JOB[file];
  const text = copy && copy.consequence ? copy.consequence : `runs ${title} in GitHub Actions against this database.`;
  return text.replaceAll("{days}", String(retentionDays ?? "the configured number of"));
}

export const JOB_NAME = {
  gardener: "Gardener run",
  "repair-propose": "Repair proposer",
  "capture-purge": "Retention purge",
  "capture-purge-dry-run": "Retention purge (dry run)",
  "webhook-index-upsert": "Incremental index upsert",
  digest: "Weekly digest",
  "digest-dry-run": "Digest preview",
  "capture-reclaim": "Lease reclaim",
  "steward-doorbell": "Steward doorbell",
};

export function jobName(word) {
  return JOB_NAME[word] || word;
}

export const DOOR = {
  admin: "this console", mcp: "MCP", slack: "Slack", cli: "the CLI", "": "an unrecorded door",
};

export function door(word) {
  return DOOR[word] ?? word;
}

// ── how each page is read ────────────────────────────────────────────────────────────────────
// `title` is the heading, `purpose` the one line under it, `read` the bullets behind "How to
// read this". Written for somebody opening the console for the first time.
export const PAGE = {
  dashboard: {
    title: "Dashboard", purpose: "the state of the brain, and what is waiting on a person",
    read: [
      "The big number is the inbox: everything a human owes a decision on. A permanent zero would "
      + "mean nobody is capturing anything.",
      "The pipeline shows what happened to what arrived in the selected window: the model drafts, "
      + "code gates, and each capture lands in git, parks on a person, or is refused.",
      "Colour is who decides — amber a human, violet the model, grey code, green git, red broke.",
    ],
  },
  inbox: {
    title: "Inbox", purpose: "everything waiting on a steward",
    read: [
      "Three kinds of item end up here: an entity the librarian proposed (a name the registry did "
      + "not know — the page already exists), a spelling it proposed for a registered entity, and a "
      + "repair proposal.",
      "This is the same list the Slack doorbell rings from — a decision taken on any door closes "
      + "the item everywhere, and the ledger's latest verdict shows here when one exists.",
      "Nothing here waits on a submitter: every capture is filed, refused or failed on its own.",
    ],
  },
  captures: {
    title: "Captures", purpose: "what people sent, and what the librarian did with it",
    read: [
      "A capture is archived the moment it arrives, then claimed by the librarian, drafted by the "
      + "agent, and gated by code. It ends filed (landed in git), declined or failed — never parked "
      + "on a person.",
      "A filed row says which page it became and which entities the librarian proposed while "
      + "filing it; a refused or failed row carries the librarian's own sentence.",
      "Reclaim and Retention purge act on the whole queue and say exactly what they will touch.",
    ],
  },
  entities: {
    title: "Entities", purpose: "the identities the librarian proposed, and the vocabulary they grow",
    read: [
      "The librarian files first and governs after: a name the registry does not know becomes an "
      + "entity page at once, marked unconfirmed, with the capture anchored to it. You approve it, "
      + "merge it into the entity it really is, or decline it — one commit each, Decided-by you.",
      "Each proposal is checked against the rest of the registry with the birth gate's own rule: "
      + "already registered, would collide, looks similar, or clear — a strong hint for Merge.",
      "Register an entity is for a name nobody has captured about yet; it is born confirmed.",
    ],
  },
  repairs: {
    title: "Repairs", purpose: "fixes the nightly proposer drafted from the gardener's findings",
    read: [
      "Every proposal is one approvable change. Approve applies exactly its edits through the "
      + "librarian's nine gates as one commit; Decline records why, which stops it being proposed "
      + "again.",
      "Read what it would change — for a drafted body the draft IS the review; for a removal, "
      + "which pages stop existing.",
      "A failed row is an apply a gate refused, kept visible with its reason.",
    ],
  },
  gardener: {
    title: "Gardener", purpose: "corpus health, on a nightly walk",
    read: [
      "Ten deterministic checks and three bounded model passes read the corpus and record "
      + "findings. It never edits the knowledge repo: it fixes nothing, publishes nothing, blocks "
      + "nothing.",
      "A partial run means the deterministic findings are complete and a model pass failed.",
      "Findings feed the Repairs proposer the next morning.",
    ],
  },
  index: {
    title: "Index", purpose: "the search index and the ops files this stack serves",
    read: [
      "The index is a cache rebuilt from the knowledge repo; the push webhook keeps it current "
      + "between nightly rebuilds.",
      "The ops files (entity registry, identity roster, Slack channel map) are served from the "
      + "index's snapshot wherever one exists — the tiles say how fresh each copy is.",
      "The substrate check lints the live index in-process; run it after registry changes.",
    ],
  },
  worker: {
    title: "Worker", purpose: "the librarian's queue, its lease and its pace",
    read: [
      "A claim is a lease. Inside the lease a worker is presumably filing; past it, the next sweep "
      + "returns the item to the queue with a delivery burned, and at the deliveries budget it "
      + "fails.",
      "Capture → filed percentiles are measured from the rows themselves.",
    ],
  },
  jobs: {
    title: "Jobs", purpose: "the four scheduled workflows, and the levers on them",
    read: [
      "Nothing scheduled runs outside GitHub Actions. Run now dispatches a workflow; Disable stops "
      + "its schedule until re-enabled (manual runs still work).",
      "Each job's truth is a database row it writes — except the index rebuild, whose truth is the "
      + "index's built_at.",
    ],
  },
  digest: {
    title: "Digest", purpose: "the week's activity in one Slack post",
    read: [
      "Command-only: no schedule exists; these buttons are the command.",
      "Each post starts where the last one ended, so nothing is covered twice and nothing is "
      + "skipped. Preview is byte-identical to what Post would send.",
    ],
  },
  activity: {
    title: "Activity", purpose: "who is using the brain, how, and how well it answers",
    read: [
      "Every MCP call leaves an audit row; the answer shape comes from the verifier's verdict — "
      + "answered with a citation, answered without one, or an honest refusal.",
      "The questions people asked are user content behind this console's one credential.",
    ],
  },
};

export function page(key) {
  return PAGE[key] || { title: key, purpose: "", read: [] };
}

export const ACTOR_HINT = "attribution, not authorization — recorded on the row's history, like --by";
