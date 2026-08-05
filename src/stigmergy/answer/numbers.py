"""Numeric token matching for the answer verifier.

Two behaviours here were paid for in observed failures, and both are worth keeping straight:

1. **The `x` multiplier.** A token regex that knows `k/m/b/bn` but not `x` tokenizes `2.3x` as the
   bare `2` — the `.3x` tail fails the trailing boundary. That was measured, not theorized: a page
   said `2.3x`, the model's draft wrote `2,3` with a decimal comma, the figure did not trace, and a correct
   page-backed answer was withheld with `unverified_figures: ['2,3']`. `x` is a DIMENSION, never a
   magnitude: `2.3x` means 2.3 (times), so it pools as 2.3 and scales nothing.

2. **Magnitude/percent laundering, closed one-sided.** `interpretations` returns both the bare
   mantissa and the scaled value for a suffixed token. An answer-side check that accepts ANY
   overlap lets `$2M` in an answer verify against a bare `2` anywhere in the evidence. So the
   ANSWER side claims the dimensioned value only (`claimed`): a magnitude-suffixed figure must
   trace to its scaled value, a percent to a percent. The EVIDENCE side stays generous (both
   readings pooled), because prose writes magnitudes out ("2,3 millones") where no tokenizer
   reaches — tightening that side would manufacture false refusals.

   **Named accepted residual**: the prose direction regressed with this fix — an answer's `$2M` no
   longer traces to evidence saying "2 millones", where the bare mantissa used to launder it
   through, which is the very hole this closes. The agent is told to quote figures as the page
   states them, so the shape of any future fix — if a real answer ever hits this — is
   EVIDENCE-side word-magnitude parsing (millones/mil/million/…), never a wider answer-side
   claim. Pinned as a named test in `tests/answer/test_numbers.py`.
"""
import re

_TOKEN = re.compile(
    r"(?<![\w.,])"
    r"(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s?(bn|[kKmMbBxX])?\s?(%)?"
    r"(?!\w)"
)
_MAGNITUDE = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}   # 'x' is deliberately absent: dimension, not scale


def _canon(v: float) -> str:
    return f"v:{v:.6g}"


def _values(num: str) -> set[float]:
    """All plausible readings of one numeric string (decimal comma vs point, ambiguous
    grouping) — the shared base `interpretations` and `claimed` both canonicalize from."""
    values: set[float] = set()
    if "." in num and "," in num:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
        thou = "," if dec == "." else "."
        try:
            values.add(float(num.replace(thou, "").replace(dec, ".")))
        except ValueError:
            pass
    elif "." in num or "," in num:
        sep = "." if "." in num else ","
        parts = num.split(sep)
        if len(parts) > 2:
            values.add(float("".join(parts)))
        else:
            head, tail = parts
            if len(tail) == 3:
                values.add(float(head + tail))
            values.add(float(head + "." + tail))
    else:
        values.add(float(num))
    return values


def interpretations(num: str, suffix: str | None, pct: str | None = None) -> set[str]:
    """All plausible values of one numeric token, canonicalized — the GENEROUS, evidence-side
    reading: a suffixed token contributes both its mantissa and its scaled value, a percent
    token both its bare value and its `%`-dimensioned one."""
    out: set[str] = set()
    for v in _values(num):
        out.add(_canon(v))
        if suffix and suffix.lower() in _MAGNITUDE:
            out.add(_canon(v * _MAGNITUDE[suffix.lower()]))
        if pct:
            out.add(_canon(v) + "%")
    return out


def claimed(num: str, suffix: str | None, pct: str | None) -> set[str]:
    """What one ANSWER token actually asserts — the STRICT, dimensioned reading (item 2 of
    the module docstring): `$2M` claims 2,000,000 and nothing else; `40%` claims forty PERCENT;
    a bare or `x`-suffixed token claims its plain value. This is the set that must intersect
    the evidence pool for the figure to ship."""
    mag = _MAGNITUDE.get((suffix or "").lower())
    if mag:
        return {_canon(v * mag) for v in _values(num)}
    if pct:
        return {_canon(v) + "%" for v in _values(num)}
    return {_canon(v) for v in _values(num)}


def number_pool(text: str) -> set[str]:
    """Every interpretation of every number in `text` (the generous, evidence-side pool)."""
    pool: set[str] = set()
    for m in _TOKEN.finditer(text or ""):
        pool |= interpretations(m.group(1), m.group(2), m.group(3))
    return pool


def unverified_figures(answer_text: str, evidence_text: str) -> list[str]:
    """Figures in the answer that no evidence interpretation backs. Bare single digits are
    skipped (list markers); repeated tokens count once. Generosity is asymmetric by design
    (module docstring): the evidence pool reads every token both ways, the answer's claim is
    dimensioned — a flag means 'this figure, AS WRITTEN, did not come from the evidence'."""
    pool = number_pool(evidence_text)
    seen: set[str] = set()
    missing: list[str] = []
    for m in _TOKEN.finditer(answer_text or ""):
        num, suffix, pct = m.group(1), m.group(2), m.group(3)
        if len(re.sub(r"\D", "", num)) == 1 and not suffix and not pct:
            continue
        display = m.group(0).strip()
        if display in seen:
            continue
        seen.add(display)
        if not (claimed(num, suffix, pct) & pool):
            missing.append(display)
    return missing
