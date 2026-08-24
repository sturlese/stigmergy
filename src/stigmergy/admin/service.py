from __future__ import annotations

import datetime as dt
import os
import tempfile
import uuid
from collections import Counter

from stigmergy.admin import schema as admin_schema
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import fetch, ops, queue, schema
from stigmergy.capture.provenance import without_capability
from stigmergy.capture.service import CaptureService
from stigmergy.changes import diff as change_diff
from stigmergy.changes import store as change_store
from stigmergy.index import health as index_health
from stigmergy.index import store as index_store
from stigmergy.knowledge import contradictions
from stigmergy.server import entity_aliases, ops_files
from stigmergy.server.identity import UNRESTRICTED_GROUP, check_group_names

DEFAULT_METRICS_DAYS = 30
MAX_LIST_LIMIT = 200
GARDEN_JOB = "garden"


class AdminBadRequest(Exception):
    pass


class AdminNotFound(Exception):
    pass


class AdminRefused(Exception):
    pass


class AdminService:
    def __init__(self, conn, *, server_settings, admin_settings: AdminSettings, evidence=None):
        self._conn = conn
        self._server = server_settings
        self._admin = admin_settings
        self._evidence = evidence
        self._principal()

    def meta(self) -> dict:
        principal = self._principal()
        groups = sorted(ops_files.known_groups(self._conn, self._server.identities_path))
        return {
            "actor": {"subject": principal.subject, "display_name": principal.display_name},
            "audiences": groups,
            "statuses": list(schema.STATUSES),
            "change_triggers": [
                "capture",
                "contradiction_resolution",
                "garden",
                "entity",
                "delete",
            ],
            "max_artifact_bytes": schema.MAX_ARTIFACT_BYTES,
        }

    def overview(self) -> dict:
        counts = queue.counts_by_status(self._conn)
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT status, min(created_at) FROM capture_queue GROUP BY status")
            oldest = {status: _iso(value) for status, value in cursor.fetchall()}
            cursor.execute("SELECT count(*) FROM knowledge_changes")
            change_count = int(cursor.fetchone()[0])
        return {
            "captures": {
                "counts": counts,
                "oldest_created_at": {status: oldest.get(status) for status in schema.STATUSES},
            },
            "changes": change_count,
            "contradictions": len(self.contradictions()["contradictions"]),
            "index": self.index_state(),
            "worker": self.worker_status(),
        }

    def captures(
        self,
        *,
        statuses: list[str] | None = None,
        submitter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        try:
            rows = queue.list_all_submissions(
                self._conn,
                statuses=statuses,
                submitter=submitter,
                limit=limit,
                offset=offset,
            )
        except ValueError as error:
            raise AdminBadRequest(str(error)) from error
        return {"count": len(rows), "captures": [self._capture_row(row) for row in rows]}

    def capture(self, capture_id: str) -> dict:
        row = queue.get_submission_trace(self._conn, capture_id)
        if row is None:
            raise AdminNotFound("capture not found")
        shaped = self._capture_row(row, detailed=True)
        if row.get("change_id"):
            shaped["change"] = self.change(row["change_id"], include_patch=False)["change"]
        return shaped

    def retry_capture(self, capture_id: str) -> dict:
        return self._mutation(
            "capture.retry",
            {"capture_id": capture_id},
            lambda: self._capture_row(queue.retry_failed(self._conn, capture_id), detailed=True),
        )

    def submit_text(
        self,
        *,
        text: str,
        title: str | None,
        occurred_at: str | None,
        audience,
        idempotency_key: str | None = None,
    ) -> dict:
        if not text:
            raise AdminBadRequest("text is required")
        principal = self._principal()
        acl = self._audience(audience)
        return self._mutation(
            "capture.submit_text",
            {"bytes": len(text.encode("utf-8")), "audience": acl},
            lambda: self._capture_receipt(
                CaptureService(self._conn, self._required_evidence()).capture_text(
                    actor=schema.Actor(
                        subject=principal.subject,
                        display_name=principal.display_name,
                    ),
                    audience=acl,
                    adapter="admin",
                    text=text,
                    idempotency_key=idempotency_key or f"admin:{uuid.uuid4()}",
                    title=_optional(title),
                    occurred_at=_occurred_at(occurred_at),
                )
            ),
        )

    def submit_url(
        self,
        *,
        url: str,
        title: str | None,
        occurred_at: str | None,
        audience,
        idempotency_key: str | None = None,
    ) -> dict:
        if not url:
            raise AdminBadRequest("URL is required")
        principal = self._principal()
        acl = self._audience(audience)
        return self._mutation(
            "capture.submit_url",
            {"audience": acl},
            lambda: self._capture_receipt(
                CaptureService(self._conn, self._required_evidence()).capture_public_url(
                    actor=schema.Actor(
                        subject=principal.subject,
                        display_name=principal.display_name,
                    ),
                    audience=acl,
                    adapter="admin",
                    url=url,
                    idempotency_key=idempotency_key or f"admin:{uuid.uuid4()}",
                    title=_optional(title),
                    occurred_at=_occurred_at(occurred_at),
                )
            ),
        )

    def submit_file(
        self,
        *,
        data: bytes,
        filename: str,
        media_type: str | None,
        title: str | None,
        occurred_at: str | None,
        audience,
        idempotency_key: str | None = None,
    ) -> dict:
        if not data:
            raise AdminBadRequest("file is required")
        principal = self._principal()
        acl = self._audience(audience)
        return self._mutation(
            "capture.submit_file",
            {"bytes": len(data), "audience": acl},
            lambda: self._capture_receipt(
                CaptureService(self._conn, self._required_evidence()).capture_bytes(
                    actor=schema.Actor(
                        subject=principal.subject,
                        display_name=principal.display_name,
                    ),
                    audience=acl,
                    adapter="admin",
                    artifact_values=((data, media_type, filename, None),),
                    idempotency_key=idempotency_key or f"admin:{uuid.uuid4()}",
                    title=_optional(title) or filename,
                    occurred_at=_occurred_at(occurred_at),
                )
            ),
        )

    def changes(self, *, trigger: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        try:
            records = change_store.list_changes(
                self._conn,
                trigger=trigger or None,
                limit=limit,
                offset=offset,
            )
        except ValueError as error:
            raise AdminBadRequest(str(error)) from error
        return {"count": len(records), "changes": [self._change_row(record) for record in records]}

    def change(self, change_id: str, *, include_patch: bool = True) -> dict:
        record = change_store.get_change(self._conn, change_id)
        if record is None:
            raise AdminNotFound("change not found")
        result = self._change_row(record)
        if include_patch:
            repo = getattr(self._server, "knowledge_repo", "") or "."
            patch = change_store.load_exact_patch(
                record,
                self._required_evidence(),
                repo=repo,
                reconstruct=self._reconstruct_patch,
            ).decode("utf-8", errors="replace")
            result["exact_patch"] = patch
            result["path_patches"] = change_diff.path_patches(patch)
        result["source_summary"] = self._source_summary(record)
        return {"change": result}

    def contradictions(self) -> dict:
        found: dict[str, dict] = {}
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT path, title, body, acl FROM pages_index WHERE type IN ('note', 'concept') ORDER BY path"
            )
            rows = cursor.fetchall()
        for path, title, body, acl in rows:
            try:
                located = contradictions.parse_all(body or "")
            except contradictions.ContradictionContractError:
                continue
            for item in located:
                value = found.setdefault(
                    item.record.contradiction_id,
                    {
                        "id": item.record.contradiction_id,
                        "explanation": item.record.explanation,
                        "claims": [claim.model_dump(mode="json") for claim in item.record.claims],
                        "paths": [],
                        "acl": acl,
                    },
                )
                value["paths"].append({"path": path, "title": title})
        values = sorted(found.values(), key=lambda value: value["id"])
        return {"count": len(values), "contradictions": values}

    def resolve_contradiction(
        self,
        *,
        contradiction_id: str,
        decision: str,
        resolution: str,
        rationale: str,
        support_url: str | None = None,
        support_file: tuple[bytes, str | None, str | None] | None = None,
    ) -> dict:
        if decision not in {"claim_a", "claim_b", "neither", "custom"}:
            raise AdminBadRequest("decision is invalid")
        if not resolution.strip() or not rationale.strip():
            raise AdminBadRequest("resolution and rationale are required")
        current = {item["id"]: item for item in self.contradictions()["contradictions"]}.get(contradiction_id)
        if current is None:
            raise AdminNotFound("contradiction not found")
        if support_url and support_file:
            raise AdminBadRequest("provide a supporting file or URL, not both")
        principal = self._principal()
        text = (
            f"Contradiction resolution\n\n"
            f"Contradiction: {contradiction_id}\n"
            f"Decision: {decision}\n\n"
            f"Resolution\n{resolution.strip()}\n\n"
            f"Rationale\n{rationale.strip()}\n"
        ).encode()
        artifacts: list[tuple[bytes, str | None, str | None, str | None]] = [
            (text, schema.MEDIA_TEXT, "resolution.txt", None)
        ]
        acquisition = None
        locator = None
        if support_url:
            original_url = without_capability(support_url)
            acquired = fetch.fetch_public(support_url)
            final_url = without_capability(acquired.final_url)
            artifacts.append(
                (
                    acquired.data,
                    acquired.response_media_type or None,
                    None,
                    final_url,
                )
            )
            locator = final_url
            acquisition = schema.AcquisitionProvenance(
                original_url=original_url,
                final_url=final_url,
                acquired_at=dt.datetime.now(dt.UTC),
            )
        elif support_file:
            data, media_type, filename = support_file
            artifacts.append((data, media_type, filename, None))
        acl = None if current["acl"] is None else tuple(current["acl"])
        return self._mutation(
            "contradiction.resolve",
            {"contradiction_id": contradiction_id, "decision": decision},
            lambda: self._capture_receipt(
                CaptureService(self._conn, self._required_evidence()).capture_bytes(
                    actor=schema.Actor(
                        subject=principal.subject,
                        display_name=principal.display_name,
                    ),
                    audience=acl,
                    adapter="admin",
                    artifact_values=tuple(artifacts),
                    idempotency_key=f"admin:contradiction:{uuid.uuid4()}",
                    title=f"Resolution for {contradiction_id}",
                    locator=locator,
                    acquisition=acquisition,
                    intent=schema.CaptureIntent(
                        resolution_of=contradiction_id,
                        rationale=rationale.strip(),
                    ),
                )
            ),
        )

    def entities(self) -> dict:
        text, origin = self._registry_source()
        try:
            payload = entity_aliases.registry_payload(text, origin)
        except ValueError as error:
            raise AdminRefused("entity registry could not be read") from error
        entities = []
        for entity_id, record in sorted(payload["entities"].items()):
            claims = sorted(
                record["claims"],
                key=lambda claim: (claim["introduced_at"], claim["claim_id"]),
                reverse=True,
            )
            entities.append(
                {
                    "id": entity_id,
                    "entity_type": record["entity_type"],
                    "created_at": record["created_at"],
                    "updated_at": record["updated_at"],
                    "claims": claims,
                    "external_ids": record["external_ids"],
                    "absorbed_ids": record["absorbed_ids"],
                }
            )
        return {
            "count": len(entities),
            "entities": entities,
            "redirects": payload["redirects"],
        }

    def entity_operation(
        self,
        *,
        action: str,
        entity_ids: list[str],
        rationale: str,
        evidence: dict | None = None,
    ) -> dict:
        principal = self._principal()
        try:
            request = schema.EntityOperationRequest(
                idempotency_key=f"admin:entity:{uuid.uuid4()}",
                actor=schema.Actor(
                    subject=principal.subject,
                    display_name=principal.display_name,
                ),
                action=action,
                entity_ids=tuple(entity_ids),
                rationale=rationale,
                evidence=evidence,
            )
        except ValueError as error:
            raise AdminBadRequest(str(error)) from error
        return self._mutation(
            f"entity.{action}",
            {"entity_ids": entity_ids, "evidence": evidence},
            lambda: self._capture_receipt(queue.enqueue_entity_operation(self._conn, request)),
        )

    def delete_pages(self, *, paths: list[str], rationale: str) -> dict:
        principal = self._principal()
        try:
            request = schema.DeleteRequest(
                idempotency_key=f"admin:delete:{uuid.uuid4()}",
                actor=schema.Actor(
                    subject=principal.subject,
                    display_name=principal.display_name,
                ),
                paths=tuple(paths),
                rationale=rationale,
            )
        except ValueError as error:
            raise AdminBadRequest(str(error)) from error
        return self._mutation(
            "knowledge.delete",
            {"paths": paths},
            lambda: self._capture_receipt(queue.enqueue_delete(self._conn, request)),
        )

    def gardener(self) -> dict:
        return {"runs": ops.list_runs(self._conn, GARDEN_JOB, limit=100)}

    def trigger_garden(self, *, rationale: str) -> dict:
        principal = self._principal()
        try:
            request = schema.GardenRequest(
                idempotency_key=f"admin:garden:{uuid.uuid4()}",
                actor=schema.Actor(
                    subject=principal.subject,
                    display_name=principal.display_name,
                ),
                rationale=rationale or "Master-triggered corpus health run",
            )
        except ValueError as error:
            raise AdminBadRequest(str(error)) from error
        return self._mutation(
            "garden.trigger",
            {},
            lambda: self._capture_receipt(queue.enqueue_garden(self._conn, request)),
        )

    def index_state(self) -> dict:
        state = index_health.read(self._conn)
        state["index_meta"] = index_store.read_meta(self._conn)
        return state

    def worker_status(self) -> dict:
        heartbeat = ops.read_heartbeat(self._conn)
        latest_change = change_store.list_changes(self._conn, limit=1)
        now = dt.datetime.now(dt.UTC)
        stale = True
        if heartbeat:
            stamp = dt.datetime.fromisoformat(heartbeat["heartbeat_at"])
            stale = now - stamp.astimezone(dt.UTC) > dt.timedelta(minutes=10)
        return {
            "heartbeat": heartbeat,
            "stale": stale,
            "last_successful_write": (self._change_row(latest_change[0]) if latest_change else None),
        }

    def activity(self) -> dict:
        return {"actions": admin_schema.recent_actions(self._conn, limit=100)}

    def _principal(self):
        principal = ops_files.resolve_identity_principal(
            self._conn,
            self._server.identities_path,
            self._admin.actor,
        )
        if UNRESTRICTED_GROUP not in principal.groups:
            raise AdminRefused("the configured admin actor is not unrestricted")
        return principal

    def _audience(self, value) -> tuple[str, ...] | None:
        if value is None:
            return None
        try:
            groups = check_group_names(
                value,
                origin="admin capture",
                subject="audience",
            )
        except Exception as error:
            raise AdminBadRequest(str(error)) from error
        if not groups:
            raise AdminBadRequest("audience cannot be empty; use null for organization-wide")
        known = ops_files.known_groups(self._conn, self._server.identities_path)
        unknown = sorted(set(groups) - known)
        if unknown:
            raise AdminBadRequest("audience contains an unknown group")
        return tuple(sorted(groups))

    def _registry_source(self) -> tuple[str | None, str]:
        snapshot = index_store.read_ops_file(self._conn, index_store.ENTITY_REGISTRY_RELPATH)
        if snapshot is not None:
            return snapshot, "index snapshot"
        path = self._server.entity_registry_path
        return entity_aliases.read_file(path), path or "entity registry"

    def _required_evidence(self):
        if self._evidence is None:
            raise AdminRefused("evidence storage is unavailable")
        return self._evidence

    def _reconstruct_patch(self, record) -> bytes:
        from stigmergy.changes.diff import exact_patch
        from stigmergy.librarian import bootstrap

        configured = getattr(self._server, "knowledge_repo", "")
        if configured and os.path.isdir(os.path.join(configured, ".git")):
            return exact_patch(configured, record.parent_commit_sha, record.commit_sha)
        repo_url = getattr(self._server, "knowledge_repo_url", "")
        if not repo_url:
            raise AdminRefused("the exact patch cache is missing and Git reconstruction is unavailable")
        with tempfile.TemporaryDirectory(prefix="stigmergy-change-") as temporary:
            checkout = os.path.join(temporary, "knowledge")
            bootstrap.ensure_checkout(
                checkout,
                url=repo_url,
                branch=getattr(self._server, "knowledge_branch", "main"),
            )
            return exact_patch(checkout, record.parent_commit_sha, record.commit_sha)

    def _capture_row(self, row: dict, *, detailed: bool = False) -> dict:
        request = row.get("request") or {}
        origin = request.get("origin") or {}
        value = {
            "id": row["id"],
            "operation": row["operation"],
            "status": row["status"],
            "submitted_by": row["submitted_by"],
            "audience": row["acl"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "processing_started_at": row["processing_started_at"],
            "finished_at": row["finished_at"],
            "source_path": row["source_path"],
            "commit_sha": row["commit_sha"],
            "change_id": row["change_id"],
            "title": origin.get("title"),
            "adapter": origin.get("adapter"),
            "error_category": row["error_category"],
            "error": row["error"],
            "report": row["report"],
        }
        if detailed:
            value.update(
                actor=row["actor"],
                provenance=origin,
                artifacts=request.get("artifacts") or [],
                extraction=row["extraction"],
                intent=request.get("intent") or {},
            )
        return value

    @staticmethod
    def _capture_receipt(row: dict) -> dict:
        return {
            "id": row["id"],
            "status": row["status"],
            "created": row.get("created", True),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _change_row(record) -> dict:
        manifest = [item.model_dump(mode="json") for item in record.manifest]
        counts = Counter(item["action"] for item in manifest)
        counts["contradictions_added"] = len({value for item in manifest for value in item["contradictions_added"]})
        counts["contradictions_resolved"] = len(
            {value for item in manifest for value in item["contradictions_resolved"]}
        )
        return {
            **record.model_dump(mode="json", exclude={"manifest"}),
            "manifest": manifest,
            "counts": dict(counts),
        }

    def _source_summary(self, record) -> dict | None:
        if record.capture_id is None:
            return None
        row = queue.get_submission_trace(self._conn, record.capture_id)
        if row is None:
            return None
        request = row.get("request") or {}
        origin = request.get("origin") or {}
        return {
            "title": origin.get("title"),
            "adapter": origin.get("adapter"),
            "captured_at": origin.get("captured_at"),
            "locator": origin.get("locator"),
            "acquisition": origin.get("acquisition"),
            "artifacts": request.get("artifacts") or [],
        }

    def _mutation(self, action: str, args: dict, function):
        actor = self._admin.actor
        try:
            value = function()
        except Exception as error:
            admin_schema.record_action(
                self._conn,
                actor=actor,
                action=action,
                args=args,
                outcome="error",
                error_class=error.__class__.__name__,
            )
            raise
        admin_schema.record_action(
            self._conn,
            actor=actor,
            action=action,
            args=args,
            outcome="ok",
        )
        return value


def _occurred_at(value: str | None) -> dt.datetime | dt.date | None:
    if not value:
        return None
    try:
        if "T" in value:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            return parsed
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise AdminBadRequest("occurred_at must be an ISO date or timezone-aware timestamp") from error


def _optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
