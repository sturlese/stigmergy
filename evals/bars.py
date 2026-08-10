"""The armed release bars — ONE home. `run_qa.py`'s `honesty_pass` and the gate itself used to
carry the same 0.90 as two separate literals, which is the classic drift pair: changing one here
changes what every reader of a QA report and the gate itself mean by PASS, and that is exactly
the point of a single home."""

BAR_RECALL = 0.80
BAR_HONESTY = 0.90
BAR_GROUNDEDNESS = 0.84          # 4.2/5, expressed on this rig's 0..1 scale

# ── the filing golden's per-facet bars (`run_filing.py`) ──────────────────────────────────────
# `None` means REPORT, DO NOT JUDGE, and every one of them starts there: these numbers are FIXED
# FROM THE FIRST BASELINE RUN against Sonnet-5, and a bar invented before its baseline exists is a
# number nobody can defend when it fails. Until the baseline row lands in `evals/history.ndjson`
# (and its numbers land in `evals/README.md` beside it), the table prints the score and no verdict
# — which is the honest state for an uncalibrated instrument, and deliberately NOT a pass.
#
# There is no bar for `attempts` or `bounces` on purpose: those are the COST axes, the same
# posture `run_qa.py` takes with retry rate and seconds/question. A backend that reaches the same
# page in two agent passes is more expensive, not worse, and folding that into a quality bar would
# measure two things through one number.
#
# `run_gates.py` does NOT read these. The filing golden files into a real git repo through a real
# agent, which makes it far more expensive than the two instruments the release gate arms; wiring
# it in is a separate decision with its own cost argument.
BAR_FILING_STATUS = None         # the terminal state each capture reached
BAR_FILING_REASON = None         # a refusal's own reason_code
BAR_FILING_TYPE = None           # the `type:` of the page that landed
BAR_FILING_FOLDER = None         # where it landed
BAR_FILING_ANCHOR = None         # the resolved registry id(s), or company-wide
BAR_FILING_EDITS = None          # the additive edits code performed from the agent's declaration
BAR_FILING_PARK_QUESTION = None  # the unresolved name a park actually captured
BAR_FILING_DECISIONS = None      # a meeting's decision pages and their independent anchors
BAR_FILING_REUSE = None          # a park did not cost the capture a decision

# One home, and a lookup so `run_filing.aggregate` does not restate the facet names a third time.
FILING_BARS = {
    "status": BAR_FILING_STATUS,
    "reason": BAR_FILING_REASON,
    "type": BAR_FILING_TYPE,
    "folder": BAR_FILING_FOLDER,
    "anchor": BAR_FILING_ANCHOR,
    "edits": BAR_FILING_EDITS,
    "park_question": BAR_FILING_PARK_QUESTION,
    "decisions": BAR_FILING_DECISIONS,
    "reuse": BAR_FILING_REUSE,
}
