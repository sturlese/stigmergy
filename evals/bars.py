"""The armed release bars — ONE home. `run_qa.py`'s `honesty_pass` and the gate itself used to
carry the same 0.90 as two separate literals, which is the classic drift pair: changing one here
changes what every reader of a QA report and the gate itself mean by PASS, and that is exactly
the point of a single home."""

BAR_RECALL = 0.80
BAR_HONESTY = 0.90
BAR_GROUNDEDNESS = 0.84          # 4.2/5, expressed on this rig's 0..1 scale

# ── the filing golden's per-facet bars (`run_filing.py`) ──────────────────────────────────────
# FIXED FROM THE FIRST SONNET-5 BASELINE (2026-08-10, sha 2b6964f, `evals/history.ndjson` — the
# run and its noise twin scored facet-identical; the full account is `evals/README.md`'s baseline
# section). A bar is the baseline's own score, with the one fractional pair floored a point
# (8/9 = 0.888… must satisfy its own bar, and a two-decimal 0.89 would refuse the very run that
# set it). The 0.88 pair tolerates exactly one type/folder disagreement — the baseline's own:
# F03 filed as a decision where the yardstick says note, a defensible reading of material that
# records a settled practice. The per-capture misses list, not the bar, is the diagnosis surface.
#
# There is no bar for `attempts` or `bounces` on purpose: those are the COST axes, the same
# posture `run_qa.py` takes with retry rate and seconds/question. A backend that reaches the same
# page in two agent passes is more expensive, not worse, and folding that into a quality bar would
# measure two things through one number.
#
# `run_gates.py` does NOT read these. The filing golden files into a real git repo through a real
# agent, which makes it far more expensive than the two instruments the release gate arms; wiring
# it in is a separate decision with its own cost argument.
BAR_FILING_STATUS = 1.00         # the terminal state each capture reached
BAR_FILING_REASON = 1.00         # a refusal's own reason_code
BAR_FILING_TYPE = 0.88           # the `type:` of the page that landed (baseline 8/9, floored)
BAR_FILING_FOLDER = 0.88         # where it landed (baseline 8/9, floored)
BAR_FILING_ANCHOR = 1.00         # the resolved registry id(s), or company-wide
BAR_FILING_EDITS = 1.00          # the additive edits code performed from the agent's declaration
BAR_FILING_PARK_QUESTION = 1.00  # the unresolved name a park actually captured
BAR_FILING_DECISIONS = 1.00      # a meeting's decision pages and their independent anchors
BAR_FILING_REUSE = 1.00          # a park did not cost the capture a decision

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
