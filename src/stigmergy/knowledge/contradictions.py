from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass

from stigmergy.knowledge.plan import ContradictionClaim, ContradictionProposal

_ID_RE = re.compile(r"^con_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_BLOCK_RE = re.compile(
    r"<!-- stigmergy-contradiction-start:(?P<id>con_[a-f0-9-]+) -->\n"
    r"(?P<visible>.*?)"
    r"<!-- stigmergy-contradiction-data:(?P<data>[A-Za-z0-9_-]+) -->\n"
    r"<!-- stigmergy-contradiction-end:(?P=id) -->",
    re.DOTALL,
)


class ContradictionContractError(ValueError):
    pass


@dataclass(frozen=True)
class Contradiction:
    contradiction_id: str
    explanation: str
    claims: tuple[ContradictionClaim, ...]


@dataclass(frozen=True)
class LocatedContradiction:
    record: Contradiction
    start: int
    end: int
    block: str


def mint_contradiction_id() -> str:
    return f"con_{uuid.uuid4()}"


def from_proposal(proposal: ContradictionProposal) -> Contradiction:
    return Contradiction(
        contradiction_id=mint_contradiction_id(),
        explanation=_one_line(proposal.explanation),
        claims=proposal.claims,
    )


def render(record: Contradiction) -> str:
    _validate(record)
    payload = {
        "id": record.contradiction_id,
        "status": "unresolved",
        "explanation": record.explanation,
        "claims": [claim.model_dump(mode="json") for claim in record.claims],
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    lines = [
        f"<!-- stigmergy-contradiction-start:{record.contradiction_id} -->",
        f"> [!WARNING] Unresolved contradiction `{record.contradiction_id}`",
        f"> {_one_line(record.explanation)}",
    ]
    for claim in record.claims:
        lines.append(f"> - **Claim:** {_one_line(claim.text)}")
        if claim.date:
            lines.append(f">   **Date:** `{_one_line(claim.date)}`")
        lines.append(f">   **Source:** `{claim.source}`")
    lines.extend(
        (
            f"<!-- stigmergy-contradiction-data:{encoded} -->",
            f"<!-- stigmergy-contradiction-end:{record.contradiction_id} -->",
        )
    )
    return "\n".join(lines)


def parse_all(text: str) -> tuple[LocatedContradiction, ...]:
    located = []
    for match in _BLOCK_RE.finditer(text):
        record = _decode(match.group("data"))
        if record.contradiction_id != match.group("id"):
            raise ContradictionContractError("contradiction ids do not match")
        block = match.group(0)
        if block != render(record):
            raise ContradictionContractError("contradiction block is not canonical")
        located.append(
            LocatedContradiction(
                record=record,
                start=match.start(),
                end=match.end(),
                block=block,
            )
        )
    starts = text.count("<!-- stigmergy-contradiction-start:")
    data = text.count("<!-- stigmergy-contradiction-data:")
    ends = text.count("<!-- stigmergy-contradiction-end:")
    if starts != len(located) or data != len(located) or ends != len(located):
        raise ContradictionContractError("contradiction marker is incomplete")
    ids = [item.record.contradiction_id for item in located]
    if len(set(ids)) != len(ids):
        raise ContradictionContractError("contradiction id is duplicated in one page")
    return tuple(located)


def append(text: str, record: Contradiction) -> str:
    if any(item.record.contradiction_id == record.contradiction_id for item in parse_all(text)):
        return text
    return f"{text.rstrip()}\n\n{render(record)}\n"


def remove(text: str, contradiction_id: str) -> tuple[str, bool]:
    matches = [item for item in parse_all(text) if item.record.contradiction_id == contradiction_id]
    if not matches:
        return text, False
    result = text
    for item in reversed(matches):
        start = item.start
        while start > 0 and result[start - 1] == "\n" and start > 1 and result[start - 2] == "\n":
            start -= 1
        result = result[:start] + result[item.end :]
    return result.rstrip() + "\n", True


def _decode(value: str) -> Contradiction:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload.get("status") != "unresolved":
            raise ContradictionContractError("contradiction status is invalid")
        record = Contradiction(
            contradiction_id=str(payload["id"]),
            explanation=str(payload["explanation"]),
            claims=tuple(ContradictionClaim.model_validate(item) for item in payload["claims"]),
        )
    except ContradictionContractError:
        raise
    except Exception as error:
        raise ContradictionContractError("contradiction data is invalid") from error
    _validate(record)
    return record


def _validate(record: Contradiction) -> None:
    if not _ID_RE.fullmatch(record.contradiction_id):
        raise ContradictionContractError("contradiction id is invalid")
    if not record.explanation.strip() or len(record.claims) < 2:
        raise ContradictionContractError("contradiction requires an explanation and two claims")
    for claim in record.claims:
        if not claim.source.startswith("sources/") or not claim.source.endswith(".md"):
            raise ContradictionContractError("contradiction source path is invalid")


def _one_line(value: str) -> str:
    return " ".join(str(value).split())
