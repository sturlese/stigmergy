"""Numeric token matching for the answer verifier.

`x` is a DIMENSION, never a magnitude: `2.3x` pools as 2.3 and scales nothing — a tokenizer
without it reads `2.3x` as a bare `2` and withholds a correct, page-backed answer.

The matching is asymmetric by design: the ANSWER side claims the dimensioned value only
(`claimed`: `$2M` must trace to 2,000,000, `40%` to a percent — any-overlap lets `$2M` verify
against a bare `2` anywhere in the evidence), while the EVIDENCE side pools both readings
(`interpretations`), because prose writes magnitudes out ("2,3 millones") where no tokenizer
reaches. An answer's `$2M` does not trace to evidence saying "2 millones"; word-magnitude
parsing belongs on the evidence side, never as a wider answer-side claim.
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
    """What one ANSWER token asserts — the STRICT, dimensioned reading: `$2M` claims 2,000,000
    and nothing else, `40%` claims forty PERCENT, a bare or `x`-suffixed token its plain value.
    This set must intersect the evidence pool for the figure to ship."""
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
