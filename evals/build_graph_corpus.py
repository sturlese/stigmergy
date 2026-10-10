#!/usr/bin/env python3
"""File fictional sources into a fresh wiki with the production librarian, one source at a time.

Builds the frozen graph-search corpus in `evals/graph_corpus/`. Each input is Markdown with a small
YAML header (`title`, `captured_at`, optional `audience`). Sources are filed oldest first through
the production planner, the writer's gates, and at most one bounded correction; a source whose
correction also fails is recorded and skipped, never retried. A source with an audience is filed in
its layer's view and projected into `layers/<layer>/`, exactly as the writer does. The run stops before a filing whose
worst-case cost would pass `--max-usd`.

  python evals/build_graph_corpus.py --inputs <dir> --live --max-usd 0.60
"""
import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import spend  # noqa: E402

from stigmergy.capture import schema  # noqa: E402
from stigmergy.capture.extraction import ExtractedArtifact, ExtractionResult  # noqa: E402
from stigmergy.capture.source import render_source  # noqa: E402
from stigmergy.kernel import layers  # noqa: E402
from stigmergy.kernel.llm import LIBRARIAN_MODEL  # noqa: E402
from stigmergy.knowledge import layered, writer  # noqa: E402
from stigmergy.knowledge.context import filing_context, render_context  # noqa: E402
from stigmergy.knowledge.lint import check  # noqa: E402
from stigmergy.knowledge.pages import path_role  # noqa: E402
from stigmergy.knowledge.write_guard import WriteContext  # noqa: E402

WORST_CASE_FILING_USD = 0.06
_NAMESPACE = uuid.UUID("6f1c2b1e-3d0a-4c35-9a52-5b8f6a0d7e11")


def _read_input(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    _, header, body = text.split("---\n", 2)
    meta = yaml.safe_load(header)
    return {"name": path.stem, "title": meta["title"], "audience": meta.get("audience"),
            "captured_at": dt.datetime.fromisoformat(str(meta["captured_at"]).replace("Z", "+00:00")),
            "text": body.strip() + "\n"}


def _render(item: dict) -> tuple[schema.CaptureEnvelope, str, str, str]:
    raw = item["text"].encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    capture_id = str(uuid.uuid5(_NAMESPACE, item["name"]))
    artifact = schema.ArtifactRef(blob_ref=schema.content_ref(digest), sha256=digest,
                                  bytes=len(raw), media_type=schema.MEDIA_MARKDOWN)
    envelope = schema.CaptureEnvelope(
        capture_id=capture_id,
        idempotency_key=f"graph-corpus:{item['name']}",
        actor=schema.Actor(subject="graph-corpus", display_name="Graph corpus builder"),
        audience=tuple(item["audience"]) if item["audience"] else None,
        origin=schema.Origin(adapter="mcp", captured_at=item["captured_at"], title=item["title"]),
        artifacts=(artifact,),
    )
    extraction = ExtractedArtifact(
        original=artifact, readable_ref=schema.content_ref(digest), readable_sha256=digest,
        readable_bytes=len(raw),
        result=ExtractionResult(text=item["text"], media_type=schema.MEDIA_MARKDOWN,
                                extractor="graph-corpus"),
    )
    at = item["captured_at"]
    path = f"sources/{at.year:04d}/{at.month:02d}/{capture_id}.md"
    open_envelope = envelope.model_copy(update={"audience": None})
    return envelope, path, render_source(envelope, (extraction,)), render_source(open_envelope, (extraction,))


def _scaffold(out: Path) -> None:
    for folder in ("notes", "concepts", "entities", "projects"):
        (out / "wiki" / folder).mkdir(parents=True, exist_ok=True)


def _charge(budget, run, phase: str) -> None:
    telemetry = run.telemetry or {}
    if not telemetry.get("output_tokens"):
        budget.charge_usd(WORST_CASE_FILING_USD, model=LIBRARIAN_MODEL, note=f"{phase}-no-usage")
        return
    budget.charge(LIBRARIAN_MODEL, int(telemetry.get("input_tokens") or 0),
                  int(telemetry["output_tokens"]), note=phase,
                  multiplier=2 if telemetry.get("hedged") else 1)


def _apply(root: str, plan, envelope, source_path: str, context: dict, layer: str):
    reasons: dict = {}
    writer._apply_filing_plan(
        root, plan, context=WriteContext(None, None, unrestricted=True),
        envelope=envelope, relative_source=source_path, reasons=reasons,
        allowed_contradiction_sources=frozenset(
            {source_path, *(item["path"] for item in context["source_evidence"])}),
        drop_broader=not envelope.intent.resolution_of,
    )
    tolerated, _ = writer._view_baseline(root, layer)
    found = check(root, editorial_paths=frozenset(p for p in reasons if path_role(p)))
    return writer._introduced(found, tolerated)


def _rejected(budget, item: dict, started: float, phase: str, error: Exception) -> dict:
    budget.charge_usd(WORST_CASE_FILING_USD, model=LIBRARIAN_MODEL, note=f"{phase}-failed")
    return {"input": item["name"], "source": None, "outcome": "rejected", "requests": None,
            "seconds": round(time.time() - started), "mutations": 0,
            "failure": [f"{phase}:{error.__class__.__name__}"]}


def _file_one(root: str, planner, budget, item: dict) -> dict:
    envelope, source_path, rendered, open_rendered = _render(item)
    layer = layers.layer_of_acl(envelope.audience)
    target = Path(root, layers.physical_path(layer, source_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    view = layered.build_view(root, layer)
    try:
        Path(view.root, source_path).parent.mkdir(parents=True, exist_ok=True)
        Path(view.root, source_path).write_text(open_rendered, encoding="utf-8")
        row = _file_in_view(view, planner, budget, item, envelope, source_path, open_rendered)
        if row["outcome"] != "rejected":
            target.write_text(rendered, encoding="utf-8")
            layered.project(view, at=envelope.origin.captured_at.date(), strict=False, skipped=[])
            row["source"] = layers.physical_path(layer, source_path)
        return row
    finally:
        layered.discard(view)


def _file_in_view(view, planner, budget, item: dict, envelope, source_path: str, rendered: str) -> dict:
    root = view.root
    envelope = envelope.model_copy(update={"audience": None})
    context = filing_context(root, source_text=rendered, capture_acl=None, actor_groups=None)
    rendered_context = render_context(context)
    started = time.time()
    try:
        run = planner.plan(worktree=root, envelope=envelope, source_path=source_path,
                           source_text=rendered, context=rendered_context)
    except Exception as error:  # noqa: BLE001 - a failed model call rejects the filing, unretried
        return _rejected(budget, item, started, "plan", error)
    _charge(budget, run, "plan")
    plan, requests, outcome = run.plan, run.model_requests, "first"
    snapshot = writer._snapshot_mutable(root)
    failure: tuple = ()
    for attempt in (1, 2):
        try:
            violations = _apply(root, plan, envelope, source_path, context, view.layer)
            failure = tuple({"path": v.path, "code": v.code, "message": v.message} for v in violations)
        except Exception as error:  # noqa: BLE001 - the writer's typed refusals all end the same way
            failure = ({"path": "plan", "code": error.__class__.__name__, "message": str(error)},)
        if not failure:
            break
        writer._restore_mutable(root, snapshot)
        if attempt == 2 or budget.spent + WORST_CASE_FILING_USD > budget.max_usd:
            outcome = "rejected"
            break
        try:
            revision = planner.revise(worktree=root, envelope=envelope, source_path=source_path,
                                      source_text=rendered, context=rendered_context, draft=plan,
                                      violations=failure, max_requests=1)
        except Exception as error:  # noqa: BLE001 - the one bounded correction failed
            return _rejected(budget, item, started, "revise", error)
        _charge(budget, revision, "revise")
        plan, requests, outcome = revision.plan, requests + revision.model_requests, "corrected"
    return {"input": item["name"], "source": source_path if outcome != "rejected" else None,
            "outcome": outcome, "requests": requests, "seconds": round(time.time() - started),
            "mutations": len(plan.mutations), "failure": [f["code"] for f in failure] if outcome == "rejected" else []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", required=True, help="directory of fictional source inputs")
    ap.add_argument("--out", default=str(ROOT / "evals" / "graph_corpus"))
    ap.add_argument("--resume", action="store_true", help="file into an existing corpus, skipping filed inputs")
    spend.add_budget_arguments(ap)
    args = ap.parse_args()
    out = Path(args.out).resolve()
    if not out.is_relative_to(ROOT / "evals") or (
            out.exists() and not (out / "filing-log.json").exists()):
        ap.error("--out must be a corpus directory under evals/ that this tool created")
    items = sorted((_read_input(p) for p in Path(args.inputs).glob("*.md")), key=lambda i: i["captured_at"])
    budget = spend.require_budget(ap, args, "graph-corpus", paid=True,
                                  expected=f"about {0.015 * len(items):.2f} USD for {len(items)} filings",
                                  models=(LIBRARIAN_MODEL,))
    from stigmergy.knowledge.planner import PydanticPlanner
    from stigmergy.librarian.config import Settings

    log_path = out / "filing-log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if args.resume and log_path.exists() else []
    done = {row["input"] for row in log}
    if not args.resume:
        if out.exists():
            shutil.rmtree(out)
        _scaffold(out)
    planner = PydanticPlanner(Settings(repo=str(out)))
    for item in items:
        if item["name"] in done:
            continue
        if budget.spent + WORST_CASE_FILING_USD > budget.max_usd:
            print(f"stopped before {item['name']}: the next filing could pass the cap", file=sys.stderr)
            break
        row = _file_one(str(out), planner, budget, item)
        log.append(row)
        log_path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**row, "spent_usd": round(budget.spent, 4)}), flush=True)
    summary = budget.summary()
    pages = sum(1 for _ in out.glob("wiki/*/*.md")) + sum(1 for _ in out.glob("layers/*/wiki/*/*.md"))
    print(json.dumps({"pages": pages, "filed": sum(1 for r in log if r["outcome"] != "rejected"),
                      "rejected": sum(1 for r in log if r["outcome"] == "rejected"), **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
