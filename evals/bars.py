"""The armed release bars — ONE home. Every reader of a report and the release gate itself must
mean the same thing by PASS, so no bar is ever spelled as a literal anywhere else."""

BAR_RECALL = 0.80
BAR_HONESTY = 0.90
BAR_GROUNDEDNESS = 0.84          # 4.2/5, expressed on this rig's 0..1 scale

# ── the filing golden's per-facet bars (`run_filing.py`) ──────────────────────────────────────
# Each bar is a recorded baseline's own score, with a fractional value FLOORED a point (8/9 =
# 0.888… must satisfy its own bar; a two-decimal 0.89 would refuse the run that set it). The 0.88
# pair therefore tolerates exactly one type/folder disagreement. Diagnosis is the per-capture
# misses list, never the bar.
#
# `attempts` and `bounces` have no bar on purpose: they are COST axes, and a backend that reaches
# the same page in two passes is more expensive, not worse.
#
# `run_gates.py` does NOT read these — the filing golden costs a real agent and a real git repo,
# so arming it in the release gate is a separate decision.
BAR_FILING_STATUS = 1.00         # the terminal state each capture reached
BAR_FILING_REASON = 1.00         # a refusal's own reason_code
BAR_FILING_TYPE = 0.88           # the `type:` of the page that landed (baseline 8/9, floored)
BAR_FILING_FOLDER = 0.88         # where it landed (baseline 8/9, floored)
BAR_FILING_ANCHOR = 1.00         # the resolved registry id(s), or company-wide
BAR_FILING_EDITS = 1.00          # the additive edits code performed from the agent's declaration
BAR_FILING_DECISIONS = 1.00      # a meeting's decision pages and their independent anchors
# `proposals` is BORN UNCALIBRATED, and `None` is the honest value rather than an omission: it is
# a facet ADR 041 created (the identity a filing proposes for a name the registry does not know),
# no recorded run has ever scored it, and a bar invented to be met is the one thing a bar may
# never be. `aggregate` reads `None` as REPORT, DO NOT JUDGE. The first real run under the
# re-frozen briefs is its baseline candidate — see evals/README.md's re-freeze rule.
BAR_FILING_PROPOSALS = None

FILING_BARS = {
    "status": BAR_FILING_STATUS,
    "reason": BAR_FILING_REASON,
    "type": BAR_FILING_TYPE,
    "folder": BAR_FILING_FOLDER,
    "anchor": BAR_FILING_ANCHOR,
    "edits": BAR_FILING_EDITS,
    "proposals": BAR_FILING_PROPOSALS,
    "decisions": BAR_FILING_DECISIONS,
}
