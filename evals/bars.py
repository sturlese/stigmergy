"""The armed release bars — ONE home. `run_qa.py`'s `honesty_pass` and the gate itself used to
carry the same 0.90 as two separate literals, which is the classic drift pair: changing one here
changes what every reader of a QA report and the gate itself mean by PASS, and that is exactly
the point of a single home."""

BAR_RECALL = 0.80
BAR_HONESTY = 0.90
BAR_GROUNDEDNESS = 0.84          # 4.2/5, expressed on this rig's 0..1 scale
