# kernel — the shared bottom of the stack

Design record: [ADR 026](../../../docs/decisions/026-the-purge.md) D4 — the whole reason this
package exists: it is what survived `stigmergy.pipeline`'s removal. This package has no
narrative doc of its own — it was extracted rather than designed, and
`kernel/__init__.py`'s module docstring is the closest thing to one. This file is the code map,
for whoever is about to edit this package rather than run it.

## Purpose

The purge removed `stigmergy.pipeline` whole — the Drive mirror, the ingest agents and worker, the
trust layer, the facts/claims/versions extractors, the corpus-distillation stages, the graph
build. What survived is here, **because the living system still calls it**. The last module kept
on a promise rather than a caller — `converters` — has since collected on that promise.

Every consumer below is the real, current import graph, not an aspiration:

| Module | Consumed by |
|---|---|
| `llm` | `gardener.sweep`, `views.synthesis` |
| `result` | `gardener.sweep`, `views.synthesis` |
| `settings` | `kernel.llm`, and nothing else — it is `llm`'s own backend parse |
| `page` | `librarian.processing`, `views.render` |
| `frontmatter` | `entities.generator` |
| `acl` | `librarian.acl_rules`, `views.regenerate`, `views.render`, `views.skeleton` |
| `registry` | `entities.birth`, `entities.generator`, `gardener.checks`, `gardener.run`, `librarian.base_inputs`, `librarian.double`, `views.cli`, `views.regenerate` |
| `normalize` | `entities.generator`, `librarian.processing` (`slugify`) |
| `fsutil` | `views.regenerate` |
| `converters` | `librarian.processing`'s **drive flow** ([ADR 028](../../../docs/decisions/028-drive-door.md) D4) — the caller this module was kept by name for. It was dead code for a long stretch and is not any more — and `capture.drive_cli`, which reads `method_for_ext` alone to decide what the door ACCEPTS, never to convert |

**`doctools.py` is DELETED.** [ADR 028](../../../docs/decisions/028-drive-door.md) D5 rejected
agent-side extraction, so the bounded `ocr` / `read_more` tool shape never got the caller it was
being held for — the Drive door converts at the worker, through `converters` alone, with no agent
in the loop. Its test suite went with it. This is the honest end of the "kept by name for a caller
that will arrive" bet: one half of the wager (`converters`) paid, the other did not, and the code
says so rather than keeping a dead contract warm.

**This package is a LIBRARY, not a layer**, exactly as `stigmergy.text` is: it
may be imported from anywhere and must import nothing from this project except itself. That rule
is pinned in `tests/test_architecture.py`
(`test_the_kernel_imports_nothing_from_this_project_except_itself`, parametrized over every module
here), not merely stated — it is what makes it safe for `capture`, `entities`, `gardener`,
`librarian` and `views` to depend on it directly, and for `server` and `slack` to reach it
transitively, with no risk of a cycle.

## Key entry points

| Module | Owns |
|---|---|
| `llm.py` | `build_model()` (`CLEAN_MODEL`/`CLEAN_REASONING_EFFORT`, call-time reads; a bare name goes to the OpenAI Responses API with an EXPLICIT reasoning effort, a `provider:model` string is resolved by pydantic-ai) and `build_processor()` — the ONE fake/real dispatch every agent-building module shares: `resolve_backend()` picks the caller's fake or a real `Agent`, and tool registration stays with the caller via the `tools` hook. The optional `model_name` lets a caller outside the `CLEAN_MODEL` convention name its own setting (today: `gardener.sweep`'s `STIGMERGY_GARDENER_MODEL`) |
| `result.py` | `fake_result(output)` — the `(.output, .usage)` shape every offline double returns from `run()`, so a fake backend needs no real usage-accounting object |
| `settings.py` | `resolve_backend()` — the ONE parse+validation of `$CLEAN_LLM` (`openai`/`fake`/`fake-flawed`), read at call time, never at import |
| `page.py` | `MAX_BODY_LINES` (150) / `SPLIT_CHUNK_LINES` (140) — the page-as-chunk contract — and `_yaml(v)`, the frontmatter scalar emitter: plain when the value provably round-trips through `yaml.safe_load`, quoted-and-escaped otherwise |
| `frontmatter.py` | `split_frontmatter(text) -> (dict, body)` — the tolerant parser; malformed or absent frontmatter degrades to `({}, text)`, never an exception |
| `acl.py` | `load_acl_config` / `load_acl_config_text`, `resolve_acl` — the ACL rule engine; `view_acl` (the members-intersection rule); `visible_to_view` (the non-member read gate); `visible` (the base visibility predicate) |
| `registry.py` | `Registry`, `load_registry` / `save_registry` — `ops/entity-registry.json`'s one reader/writer, plus the `canonical_id` / `title` / `type_of` lookups |
| `normalize.py` | `normalize(name)` (canonical key: accents folded, lowercased, legal suffixes stripped iteratively), `slugify(s)` (≤60 chars), `is_noise(norm_key)` |
| `fsutil.py` | `write_text_atomic(path, text)` — tmp file + same-directory `os.replace`, so a concurrent reader never sees a partial write |
| `converters.py` | `method_for_ext`, `extract` (pdf/sheet/docx/office/text → `{method, text}`), `sheet_rows`, `vision_extract` (Gemini OCR, lazy SDK import) — the document HANDS; no judgment, just faithful text |

## Use these

- **`llm.build_processor`** — the ONE fake/real agent dispatch. Its two callers,
  `views.synthesis.build_view_agent` and `gardener.sweep.build_judge`, both go through it rather
  than re-typing the `resolve_backend()` branch; a new agent-building module in ANY package does
  the same.
- **`result.fake_result`** — the one offline-double result envelope. A new fake backend returns
  this, never a hand-rolled namespace with `.output`/`.usage`.
- **`page._yaml`** — the one frontmatter scalar emitter. `views.render` imports it directly rather
  than re-deriving the plain-vs-quoted decision; so does any new frontmatter writer.
- **`frontmatter.split_frontmatter`** — the tolerant parser for a caller that does not need the
  full page contract. `entities.generator` uses it; `index.corpus` deliberately keeps its own,
  stricter one for indexing.
- **`acl.resolve_acl` / `view_acl` / `visible_to_view`** — the one ACL rule engine and its two
  view-specific predicates. `librarian.acl_rules` is a DIALECT ADAPTER over `resolve_acl` (the
  on-disk `ops/acl.json` format differs from what this module's loader expects) — a new caller
  with a differently-shaped ACL source writes its own adapter the same way, never a second
  resolution algorithm.
- **`registry.load_registry` / `save_registry`** — the ONE reader/writer of
  `ops/entity-registry.json`. Every package that needs the registry reads through this, never a
  hand-rolled JSON parse.
- **`normalize.normalize` / `slugify`** — the one canonicalization pair. They answer DIFFERENT
  questions (`slugify` = what a page titled `name` regenerates as; `normalize` = the matching key
  legal-suffix stripping folds onto); see `entities.generator.canonical_id_for`'s docstring for
  why confusing them is a real defect.
- **`fsutil.write_text_atomic`** — the one atomic write, for any file another process might read
  mid-write. Never a plain `open(path, "w")`.

## Avoid / anti-patterns

- **Never import anything from `stigmergy.*` other than `stigmergy.kernel` itself from inside this
  package.** Mechanically pinned over every module here
  (`test_the_kernel_imports_nothing_from_this_project_except_itself`) — the day this package needs
  a stigmergy import it has stopped being the bottom of the stack, and whatever it wanted belongs in
  the CALLING package.
- **Never load a provider SDK (`pydantic_ai.models`/`.providers`, `google.genai`) at module
  level.** Pinned by `test_the_kernel_never_imports_an_agent_framework_at_module_level`:
  `llm.build_model` imports the OpenAI classes inside the function body and
  `converters.vision_extract` imports the Gemini SDK inside itself, so a keyless offline run
  (`CLEAN_LLM=fake`) pays for neither.
- **Never read the environment anywhere but at call time.** A module-level `os.environ.get` here
  would be resolved once, by whichever process imports it first, for every caller in the codebase.
- **Do not rename `acl._MATCHERS` or `acl._check_labels` casually.** They are private, but
  `librarian.acl_rules` reaches both to translate the knowledge repo's on-disk dialect without a
  second matching algorithm. That coupling is deliberate and pinned by two tests
  (`test_the_acl_adapters_reach_into_pipelines_private_names_is_pinned`,
  `test_the_acl_private_names_still_have_the_shape_the_adapter_assumes`) precisely so a rename
  fails a test instead of breaking the librarian at worker STARTUP.
- **`acl.visible` is not the server's enforcement point.** `stigmergy.server.acl.visible` is a
  separate, deliberately STRICTER implementation — it treats a malformed stored value as hidden
  from everyone, including unrestricted clients, and it accepts the legacy CSV shape. It mirrors
  this function rather than importing it, and the divergence is documented in its own docstring.
  Do not "unify" them without re-deciding the fail-closed rule.
- **`acl.decode_csv_acl` is DELETED**, together with the facts store's CSV `acl` column it decoded.
  Finding the name in the history and wanting it back means the facts store is being rebuilt —
  which needs its contract re-decided, not merely re-wired.
- **Never let a consumer re-derive a registry, ACL or normalization rule instead of importing it
  from here.** ONE definition per concern is the entire reason this package was extracted.

## Data & contracts

- **`registry.Registry`** (plain dataclass) — `entities: dict` (id → `{name, type, aliases}`) and
  `by_alias: dict` (normalized alias/name/id → id, built from the id, the display name and every
  alias). `canonical_id(name)` / `title(id)` / `type_of(id)` are the three lookups consumers need;
  nothing outside this module builds one by hand. `load_registry` treats a missing path/file as an
  empty registry and a malformed one as a loud error; `save_registry` writes atomically, sorted and
  alias-deduplicated.
- **`ops/entity-registry.json`** — the on-disk shape (`{"entities": {"<id>": {"name", "type",
  "aliases"}}}`). Human-owned, diffable, and written by exactly ONE program: `stigmergy-entities`.
- **`page._PLAIN_YAML` + `_yaml`'s round-trip proof** — a scalar is emitted UNQUOTED only when it
  matches a restricted charset AND `yaml.safe_load` reads it back as the identical string. The
  round-trip IS the check: it catches every YAML 1.1 implicit type (a bare date, a hex or
  underscored int, `true`/`on`/`~`) that a hand-maintained pattern list silently misses, and an
  invalid date that matches the timestamp regex but raises on construction falls through to quoted.
- **`converters.EXT_METHOD`** — the one extension → method table (`pdf`, `sheet`, `docx`, `office`,
  `text`); `method_for_ext` is its only reader, defaulting to `text`. `.ods` routes through
  `office` (LibreOffice via Gotenberg), never `openpyxl`, which cannot read OpenDocument
  spreadsheets at all. Grids are capped at `SHEET_MAX_ROWS` (5000) with a `SHEET_SAMPLE_ROWS` (25)
  profile shown to the model.
- **`extract` returns text that can hard-wrap a long token.** The `pdf` and `office` paths both run
  `pdftotext -layout`, which breaks at layout boundaries rather than word boundaries, so a
  credential inside a dropped document reaches the worker ALREADY split across a line break —
  and gitleaks only ever matches within one line. That is the failure `librarian.gates` scans every
  surface twice for (as written, and with adjacent line PAIRS rejoined); changing what these
  converters emit changes what that gate can see.
- **`converters.vision_extract`** — reads `VISION_MODEL` (default `gemini-3-flash-preview`) and
  requires `GEMINI_API_KEY`. PDFs ≤14 MB go inline as bytes (no Files API, so a non-ASCII filename
  cannot break the ASCII header encoding); larger ones upload through a temp copy with an ASCII
  name. The model id is returned as provenance.
- **`acl` truth table** — `resolve_acl` returns the first matching rule's audiences, else the
  config default, else `None` when ACLs are off. `view_acl` intersects members' audiences (a
  rollup must never widen access); all-`None` members yield `None` (open); an empty intersection is
  restrictive by construction. `visible_to_view` gates a NON-member governed source: an open row is
  visible anywhere, an open view admits only open rows, and a narrowed view admits a restricted row
  only when `set(view_acl) <= set(row_acl)`.

## Tests

`tests/kernel/` — 7 test modules, one per module that has a dedicated suite (`test_doctools.py` was
deleted alongside `doctools.py`):

| Suite | Covers |
|---|---|
| `test_acl.py` | config validation (including the CSV-breaking label rule), first-match-wins resolution, the view intersection, the `visible` truth table |
| `test_registry.py` | the alias map, missing-is-empty / malformed-is-loud, alias merging across `normalize` boundaries, the sorted save round-trip |
| `test_page.py` | hostile scalars surviving the frontmatter round-trip, YAML 1.1 implicit types staying strings, plainly-safe scalars staying unquoted, the split budget leaving room for per-part chrome |
| `test_settings.py` | the default, case-insensitive valid backends, the loud rejection, and that the read happens at CALL time |
| `test_llm.py` | the fake and fake-flawed paths, fail-fast before any construction on an unknown backend, and that the `tools` hook runs on the real path and NOT on the fake one |
| `test_converters.py` | extension routing, table escaping, csv/xlsx grids, the sheet profile, pdf/office/docx extraction and their failure modes, vision fail-fast without a key and the inline small-PDF path |
| `test_fsutil.py` | parent-dir creation, and replacement leaving no `.tmp` behind |

**Not covered by a dedicated suite**: `normalize.py`, `frontmatter.py` and `result.py` — they are
exercised only transitively, through the consumers listed in Purpose. `normalize` in particular
carries the legal-suffix table that entity identity depends on, and it is the most load-bearing of
the three; it is the gap worth closing first.

Layering is pinned separately, in `tests/test_architecture.py`:
`test_the_kernel_imports_nothing_from_this_project_except_itself`,
`test_the_kernel_never_imports_an_agent_framework_at_module_level`, `test_the_pipeline_package_is_gone`
(a size-1 regression guard), the two ACL private-name tests, and
`test_review_transitive_kernel_reach_is_a_named_declared_exception`.

## Common tasks

| Task | Touch |
|---|---|
| Add a new agent-building module anywhere in the codebase | `llm.build_processor` — never re-type the `resolve_backend()` / fake-vs-real branch |
| Change the page-as-chunk split thresholds | `page.MAX_BODY_LINES` / `SPLIT_CHUNK_LINES` — read by `librarian.processing`'s splitter |
| Change how a frontmatter scalar is quoted | `page._yaml`'s round-trip proof (`_PLAIN_YAML`) — every frontmatter writer depends on it staying faithful |
| Change the ACL resolution algorithm | `acl.resolve_acl` / `load_acl_config_text` — `librarian.acl_rules` sits ABOVE this and must not grow its own matching |
| Change entity-name canonicalization | `normalize.normalize` / `slugify` / `is_noise` — no dedicated test suite guards this yet, so add one with the change |
| Add a new document conversion method | `converters.EXT_METHOD` + a `_<method>` function wired into `extract`, plus a case in `tests/kernel/test_converters.py` |
| Add a document conversion the drive door should admit | `converters.EXT_METHOD` + a `_<method>` function wired into `extract`, a case in `tests/kernel/test_converters.py`, AND the door's own format policy (`capture.drive_cli`) — the door decides what it accepts, this package decides how it converts |

## Notes

- **`converters` earned its keep; `doctools` did not.** Both were kept by name, through the purge,
  for a caller that had not arrived. One arrived: `librarian.processing`'s drive flow converts at
  the worker through `extract`, proven end to end over the real `pdftotext` binary in
  `tests/librarian/test_drive_processing_pg.py`.
  [ADR 028](../../../docs/decisions/028-drive-door.md) D5 rejected agent-side extraction, so
  `doctools` — the bounded `ocr`/`read_more` agent-tool shape — was deleted instead of waiting
  longer. Worth remembering as a precedent for the next "keep it, something will call it" argument:
  name the change that has to justify it, and delete it there if it cannot.

- **`stigmergy.text` and `stigmergy.review_kinds` are this package's siblings at the bottom of the
  stack**, not part of it. All three import nothing from this project, and each has a test saying
  so (`test_stigmergy_text_is_the_bottom_of_the_stack`,
  `test_stigmergy_review_kinds_is_the_bottom_of_the_stack`). See
  [`../../../docs/reference/answer.md`](../../../docs/reference/answer.md) and the `server`/`slack`
  maps for who depends on them.
- **Every consumer's declared reach into this package is narrower than "all of kernel", and that
  is enforced per caller.** `stigmergy.server.review`'s transitive reach, for example, is pinned to
  exactly `{kernel, kernel.acl, kernel.frontmatter, kernel.normalize, kernel.registry}` by a
  subprocess-based test. A change here that widens what a consumer transitively pulls in is what
  those tests exist to catch.
- **`normalize.py` is the gap worth closing first.** It has no dedicated suite (see Tests) and it
  carries the legal-suffix table entity identity depends on: `entities.generator`'s
  duplicate-match-key check folds names through it directly, and `entities.birth`'s collision gate
  reaches it through `registry.Registry.canonical_id`. A false negative there admits a duplicate
  entity past a governance gate, which is the one failure in this package with no second line of
  defense. `server.entity_aliases` deliberately does NOT use it — it carries its own, looser
  `_norm` (no legal-suffix stripping), because recognizing a registered name inside a QUESTION is
  a retrieval nicety whose false negative costs a fallback to semantic search, not an identity
  decision; its module docstring is the ruling.
