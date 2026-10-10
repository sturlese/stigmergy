export type PageType = "note" | "concept" | "entity" | "project" | "source";

export interface WorkspaceRef { id: string; slug: string; name: string }

export interface Me {
  subject: string;
  display_name: string;
  kind: "person" | "agent";
  is_admin: boolean;
  groups: string[];
  unrestricted: boolean;
  default_layer: string | null;
  layers: string[];
  /** A platform operator: reaches the master console and acts in one workspace at a time. */
  operator: boolean;
  /** The workspace this session acts in; null for an operator outside every workspace. */
  tenant: WorkspaceRef | null;
}

export type WorkspaceStatus = "provisioning" | "active" | "suspended";

export interface PlatformWorkspace extends WorkspaceRef {
  status: WorkspaceStatus;
  repo: string;
  slack_team_id: string | null;
  people: number;
  pending: number;
  failed: number;
  last_landed_at: string | null;
  index_dirty: boolean | null;
  last_garden_at: string | null;
  created_at: string | null;
  spend_7d: { input_tokens: number; output_tokens: number; usd: number };
  erasure: WorkspaceErasure | null;
}

export interface WorkspaceErasure {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  attempts: number;
  error: string;
  requested_at: string | null;
  next_attempt_at: string | null;
  repo_deleted: boolean;
  rows_deleted: boolean;
}

export interface AccessRequest {
  id: string;
  email: string;
  company: string;
  note: string;
  status: "new" | "handled";
  created_at: string | null;
  handled_by: string;
  handled_at: string | null;
}

export interface PlatformAction {
  id: number;
  ts: string | null;
  actor: string;
  action: string;
  tenant: string;
  outcome: string;
}

export interface PlatformWriter {
  heartbeat: { heartbeat_at: string; state: string } | null;
  failed: { tenant: string; id: string; operation: string; finished_at: string | null; error_category: string }[];
  actions: PlatformAction[];
}

export interface CreatedWorkspace {
  tenant: WorkspaceRef & { status: WorkspaceStatus; repo: string };
  admin: string;
  token: { token: string; prefix: string; subject: string };
}

export interface CatalogPage {
  path: string;
  title: string;
  type: PageType;
  entity_type: string;
  aliases: string[];
  updated: string;
  layers: string[];
  summary: string;
}

export interface Catalog {
  count: number;
  truncated: boolean;
  pages: CatalogPage[];
}

export interface PageRef {
  path: string;
  title: string;
  type: PageType;
}

export interface GraphData {
  nodes: (PageRef & { entity_type?: string; layers?: string[]; weight?: number })[];
  edges: { from: string; to: string }[];
  truncated?: boolean;
}

export interface WikiPage {
  path: string;
  title: string;
  type: PageType;
  entity_type: string;
  aliases: string[];
  status: string;
  updated: string;
  layers: string[];
  truncated: boolean;
  html: string;
  links: PageRef[];
  backlinks: PageRef[];
  cited: PageRef[];
  graph: GraphData;
}

export interface SearchHit {
  path: string;
  title: string;
  type: PageType;
  summary: string;
  snippet: string;
  updated: string;
  layers: string[];
}

export interface WikiStatus {
  dirty: boolean;
}

export interface Person {
  subject: string;
  display_name: string;
  kind: "person" | "agent";
  is_admin: boolean;
  groups: string[];
  default_audience: string[] | null;
  unrestricted: boolean;
  disabled: boolean;
  live_tokens: number;
  last_used_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface Token {
  id: string;
  label: string;
  prefix: string;
  created_at: string | null;
  created_by: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  live: boolean;
}

export interface TokenGrant {
  token: string;
  id: string;
  subject: string;
  label: string;
  prefix: string;
  expires_at: string | null;
}

export interface Group {
  name: string;
  description: string;
  created_at: string | null;
  members: number;
  unrestricted: boolean;
  erasing: boolean;
}

export interface LayerInventory {
  layer: string;
  pages: Record<string, number>;
  pages_total: number;
  captures: number;
  originals: number;
  changes: number;
}

export interface ErasureReceipt {
  old_head: string;
  new_head: string;
  commits_removed: number;
  commits_rewritten: number;
  refs_rewritten: number;
  pages_indexed: number;
  captures_deleted: number;
  operations_failed: number;
  objects_deleted: number;
  changes_deleted: number;
  changes_rewritten: number;
  changes_remapped: number;
  rows_redacted: number;
  remaining: { what: string; until: string; link?: string; first_changed_commits?: string[] }[];
}

export interface LayerErasure {
  id: string;
  layer: string;
  requested_by: string;
  requested_at: string;
  status: "queued" | "running" | "succeeded" | "failed";
  attempts: number;
  next_attempt_at: string;
  pushed: boolean;
  started_at: string | null;
  finished_at: string | null;
  error: string;
  receipt: ErasureReceipt | Record<string, never>;
}

export interface Channel {
  channel_id: string;
  groups: string[] | null;
  updated_at: string | null;
  updated_by: string;
}

export type CaptureStatus = "queued" | "processing" | "landed" | "failed";

export interface Meta {
  actor: { subject: string; display_name: string };
  layers: string[];
  default_layer: string | null;
  statuses: CaptureStatus[];
  change_triggers: string[];
  max_artifact_bytes: number;
  layer_threshold: number;
}

export type LayerDecision = "explicit" | "channel" | "default" | "classified";

export interface Artifact {
  original_name?: string | null;
  media_type?: string;
  bytes?: number;
  sha256?: string;
}

export interface CaptureRow {
  id: string;
  operation: string;
  status: CaptureStatus;
  submitted_by: string;
  audience: string[] | null;
  layer_decision: LayerDecision | null;
  attempts: number;
  created_at: string | null;
  processing_started_at: string | null;
  finished_at: string | null;
  source_path: string | null;
  commit_sha: string | null;
  change_id: string | null;
  title: string | null;
  adapter: string | null;
  error_category: string | null;
  error: string | null;
  report: unknown;
}

export interface CaptureDetail extends CaptureRow {
  actor?: { subject?: string; display_name?: string } | null;
  layer_probabilities?: Record<string, number> | null;
  provenance?: {
    adapter?: string; occurred_at?: string | null; locator?: string | null; title?: string | null;
    captured_at?: string | null;
    acquisition?: {
      original_url?: string; final_url?: string; acquired_at?: string; drive_file_id?: string;
      drive_media_type?: string; export_media_type?: string;
    } | null;
  } | null;
  artifacts?: Artifact[];
  extraction?: unknown;
  intent?: Record<string, unknown>;
  change?: ChangeRow;
}

export interface CaptureReceipt { id: string; status: CaptureStatus; created: boolean; created_at: string | null }

export interface PathChange {
  path: string;
  action: "created" | "updated" | "deleted";
  page_role: "note" | "concept" | "entity" | "project" | "source" | "ops";
  reason: string;
  additions: number;
  deletions: number;
  contradictions_added: string[];
  contradictions_resolved: string[];
}

export interface ChangeRow {
  id: string;
  trigger: string;
  actor: string;
  capture_id: string | null;
  parent_commit_sha: string;
  commit_sha: string;
  summary: string;
  manifest: PathChange[];
  exact_patch_bytes: number;
  created_at: string | null;
  counts: Record<string, number>;
}

export interface ChangeDetail extends ChangeRow {
  exact_patch?: string;
  path_patches?: Record<string, string>;
  source_summary?: {
    title?: string | null; adapter?: string; captured_at?: string | null; locator?: string | null;
    acquisition?: { original_url?: string; final_url?: string; drive_file_id?: string } | null;
    artifacts: Artifact[];
  } | null;
}

export interface Contradiction {
  id: string;
  explanation: string;
  claims: { text: string; date?: string | null; source: string }[];
  paths: { path: string; title: string }[];
  acl: string[] | null;
}

export interface EntitySummary {
  path: string;
  title: string;
  entity_type: string;
  aliases: string[];
  layers: string[];
}

export interface Dossier {
  found: boolean;
  entity: (EntitySummary & { status?: string; updated?: string; excerpt?: string; excerpt_truncated?: boolean }) | null;
  knowledge: {
    path: string; title: string; type: string; updated?: string; excerpt?: string;
    sources?: { path: string; title: string }[];
    relationships?: { path: string; title: string; statement: string }[];
  }[];
  knowledge_note?: string;
  sources: { path: string; title: string }[];
}

export interface JobRun {
  id: string;
  job: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  base_commit_sha: string | null;
  head_commit_sha: string | null;
  stats: Record<string, number | string | null>;
  error_category: string | null;
  error: string | null;
}

export interface IndexState {
  repository_head_sha: string | null;
  indexed_commit_sha: string | null;
  dirty: boolean;
  dirty_since: string | null;
  last_incremental_at: string | null;
  last_full_rebuild_at: string | null;
  indexed_rows: number;
  updated_at: string | null;
  warnings: string[];
  healthy: boolean;
  index_meta?: { model?: string; dim?: number; fts_config?: string; built_at?: string | null; host?: string } | null;
  filing_context?: { captures: number; max_share: number; trimmed: number; due: boolean };
}

export interface GraphMeasureValues {
  pages: number;
  pages_by_type: Record<PageType, number>;
  links_per_page: number;
  dead_links: number;
  orphan_entities: number;
  multi_source_entities: number;
  cited_paragraphs: number;
  entities_without_aliases: number;
  thin_entities: number;
  entities: number;
}

export interface MeasuredPage extends PageRef {
  entity_type: string;
  inbound: number;
  layers: string[];
  facts?: number;
}

export interface GraphMeasures {
  scope: string;
  scopes: string[];
  measures: GraphMeasureValues;
  hubs: MeasuredPage[];
  lists: { orphan_entities: MeasuredPage[]; thin_entities: MeasuredPage[]; entities_without_aliases: MeasuredPage[] };
  constellation: { nodes: (PageRef & { inbound: number; orphan: boolean })[]; edges: { from: string; to: string }[] };
  series: { taken_at: string; indexed_commit: string; measures: GraphMeasureValues }[];
  week_ago: GraphMeasureValues | null;
}

export interface WorkerStatus {
  heartbeat: { state?: string; heartbeat_at?: string } | null;
  stale: boolean;
  last_successful_write: ChangeRow | null;
}

export interface Overview {
  captures: { counts: Partial<Record<CaptureStatus, number>>; oldest_created_at: Partial<Record<CaptureStatus, string | null>> };
  changes: number;
  contradictions: number;
  index: IndexState;
  worker: WorkerStatus;
}

export interface AdminAction {
  id: number;
  ts: string | null;
  actor: string;
  action: string;
  args: Record<string, unknown>;
  outcome: string;
  error_class: string;
}
