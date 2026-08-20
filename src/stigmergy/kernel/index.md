# kernel — the shared bottom of the stack

A LIBRARY, not a layer: importable from anywhere, and it imports nothing from this project except
itself. `stigmergy.text` and `stigmergy.review_kinds` are siblings at the bottom of the stack, not
part of it. The layering, the no-SDK-at-module-level rule and each consumer's declared reach are
pinned in `tests/test_architecture.py`; per-module suites live in `tests/kernel/`.

## Modules

| Module | What it is |
|---|---|
| `llm.py` | `build_model()` — the two-form convention's ONE implementation: a bare name is the OpenAI Responses API with an EXPLICIT reasoning effort, a `provider:model` string is resolved by pydantic-ai, whose provider reads its own key. The caller may NAME both the model and its own `reasoning_effort`; CLEAN_MODEL / CLEAN_REASONING_EFFORT are the fallback, read at call time. `build_processor()` — the ONE fake/real agent dispatch; tool registration stays with the caller via the `tools` hook; optional `model_name` for a caller outside the CLEAN_MODEL convention; `model_override(model)` is the PUBLIC test seam — an explicit pydantic-ai model object (a `FunctionModel`/`TestModel`) that `build_model` answers with inside the block, so any package proves a tool-loop property against the real Agent, keyless, without reaching into this module |
| `result.py` | `fake_result(output)` — the `(.output, .usage)` envelope every offline double returns from `run()` |
| `usage_repair.py` | `ensure_usage_extraction_repaired()` — the shim for the pinned pydantic-ai's silent all-zero token extraction on OpenAI reasoning models; idempotent, call-time, defers to the original so it retires itself on a fixed version; installed by every agent-construction site in the process |
| `settings.py` | `resolve_backend()` — the ONE parse+validation of `$CLEAN_LLM` (`openai`/`fake`/`fake-flawed`), read at call time — plus `PROVIDER_KEY_ENV`/`provider_of`, the one provider→key table and prefix predicate (framework-free, so keyless modules can consult them; the librarian's preflight re-exports both) |
| `page.py` | `MAX_BODY_LINES` / `SPLIT_CHUNK_LINES` — the page-as-chunk contract — and `_yaml(v)`, the frontmatter scalar emitter: plain only when the value provably round-trips through `yaml.safe_load`, quoted-and-escaped otherwise |
| `frontmatter.py` | `split_frontmatter(text) -> (dict, body)` — tolerant: malformed or absent frontmatter degrades to `({}, text)`, never an exception |
| `acl.py` | `load_acl_config` / `load_acl_config_text`, `resolve_acl` (first matching rule wins), `view_acl` (members INTERSECTION — a rollup must never widen access), `visible_to_view` (the non-member read gate) |
| `registry.py` | `Registry`, `load_registry` / `registry_from_text` / `save_registry` / `index_entity` — `ops/entity-registry.json`'s one reader/writer, plus `title` / `type_of` and the TWO lookups the registry is asked for: `canonical_id` (which entity does this text MEAN — filing) and `collision_id` (would this new name be confused with one we have — the mint gate). Missing file = empty registry; malformed = loud error. The reader is split path-from-text because the registry also reaches a reader as BYTES now (the index's snapshot, which `index.check` lints through this same parse) |
| `normalize.py` | `resolution_key(name)` (accents, case and punctuation folded — and nothing that is a judgment), `normalize(name)` (that plus the legal-suffix table: the COLLISION key), `slugify(s)` (≤60 chars) |
| `fsutil.py` | `write_text_atomic(path, text)` — tmp file + same-directory `os.replace`, so a concurrent reader never sees a partial |
| `converters.py` | the document HANDS: `method_for_ext`, `extract` (pdf/sheet/docx/office/text → `{method, text}`), `sheet_rows`, `vision_extract` (two-form OCR: bare = Gemini native-PDF, provider-prefixed = pydantic-ai over rasterized pages; lazy SDK imports). Faithful text, no judgment |

## Reuse — one definition per concern

- `llm.build_model` for a caller that resolves a model of its OWN: `answer.synthesize` names
  ANSWER_MODEL and that call's reasoning effort through its parameters instead of carrying a copy
  of the bare-vs-prefixed branch, and gets the usage repair and the `model_override` seam with it.
  A second spelling of the two-form convention is how two surfaces start disagreeing about what a
  model string means.
- `llm.build_processor` for any new agent-building module, anywhere — never re-type the
  fake-vs-real branch.
- `result.fake_result` for any new fake backend — never a hand-rolled `.output`/`.usage` namespace.
- `page._yaml` for any frontmatter writer — never re-derive the plain-vs-quoted decision.
- `frontmatter.split_frontmatter` for a caller that does not need the full page contract
  (`index.corpus` deliberately keeps its own, stricter parser).
- `acl.resolve_acl` / `view_acl` / `visible_to_view` — a caller with a differently-shaped ACL
  source writes an adapter over these (`librarian.acl_rules` is one), never a second resolution
  algorithm.
- `registry.load_registry` / `registry_from_text` / `save_registry` — never a hand-rolled JSON
  parse of the registry, from a path or from bytes. `registry.index_entity` for anything that
  BUILDS a `Registry` in memory (`entities.generator._index` is the other caller): it is the one
  place either lookup key is computed, and hand-filling `by_alias` keys one map and silently leaves
  the other empty.
- `normalize.resolution_key` / `normalize.normalize` / `slugify` — THREE keys, three questions.
  `slugify` is the id a page regenerates as; `resolution_key` is what a capture resolves through
  (accents, case, punctuation — nothing a developer could be wrong about); `normalize` is
  `resolution_key` plus the legal-suffix table, and it exists for the mint gate alone (see
  `entities.birth._refuse_collisions` and `entities.generator.canonical_id_for`).
- `fsutil.write_text_atomic` for any file another process might read mid-write.

## Avoid

- Importing anything from `stigmergy.*` except `stigmergy.kernel` itself. The day this package
  needs a stigmergy import, whatever it wanted belongs in the CALLING package.
- Loading a provider SDK at module level: `llm.build_model` and `converters.vision_extract`
  import theirs inside the function body, so a keyless offline run pays for neither.
- Reading the environment anywhere but at call time.
- Renaming `acl._MATCHERS` or `acl._check_labels` casually: `librarian.acl_rules` reaches both to
  translate its on-disk dialect, and two architecture tests pin the coupling so a rename fails a
  test instead of breaking the librarian at worker startup.
- "Unifying" `acl` with `stigmergy.server.acl.visible` — the server's is a deliberately STRICTER
  separate implementation (a malformed stored value hides from everyone) that mirrors this module
  rather than importing it.
- Letting a consumer re-derive a registry, ACL or normalization rule instead of importing it.

## Contracts

- ACL truth table: `resolve_acl` returns the first matching rule's audiences, else the config
  default, else `None` when ACLs are off. `view_acl`: members without ACLs don't restrict,
  all-`None` yields `None` (open), an empty intersection is restrictive by construction.
  `visible_to_view`: an open row renders anywhere; an open view admits only open rows; a narrowed
  view admits a restricted row only when `set(view_acl) <= set(row_acl)`.
- `converters.EXT_METHOD` is the one extension→method table (`method_for_ext` its only reader,
  defaulting to `text`). `.ods` routes through `office` (LibreOffice via Gotenberg) — openpyxl
  cannot read OpenDocument spreadsheets. Grids cap at `SHEET_MAX_ROWS` with a `SHEET_SAMPLE_ROWS`
  profile shown to the model.
- `extract`'s pdf/office paths run `pdftotext -layout`, which can hard-wrap a long token across a
  line break — so a credential in a dropped document can reach the worker already split, and
  gitleaks matches only within one line. `librarian.gates` scans every surface twice (as written,
  and with adjacent line pairs rejoined) for exactly that; changing what these converters emit
  changes what that gate can see.
- `converters.vision_extract` reads `VISION_MODEL`, two forms. BARE (the default
  `gemini-3-flash-preview`): Gemini native PDF, requires `GEMINI_API_KEY`; PDFs ≤14 MB go inline
  as bytes (the Files API's ASCII header encoding breaks on non-ASCII filenames), larger ones
  upload through an ASCII-named temp copy. PROVIDER-PREFIXED
  (`openrouter:qwen/qwen3-vl-8b-instruct`): pdftoppm-rasterized page images through pydantic-ai
  — output box bounded by `-scale-to` (a fixed DPI is a raster bomb on a max-MediaBox page),
  both subprocesses and the model call on their own clocks, at most `MAX_VISION_PAGES` pages
  with a spoken cut and `pages`/`truncated` returned as data beside it. Either form returns the
  configured id as provenance and the pass's token `usage` (`None` when the framework reported
  none — absent is honest where a zero reads as free), which is what lets `librarian.processing`
  price an OCR exactly as it prices a filing pass. `vision_config_error` is the one answer to "can
  this run, and if not why" (`librarian.processing` asks it before paying for a call; a KNOWN prefix
  with no key is unconfigured with the variable named, an unknown prefix is configured by
  naming itself).
- `normalize.py`'s suite is `tests/kernel/test_normalize.py`, added with the split that gave it a
  second key; `frontmatter.py`'s lives in `tests/kernel/test_frontmatter.py`. Every case there is
  written against a spelling that DISCRIMINATES the two keys — one both answer alike proves nothing
  about the line between them.
- **The legal-suffix table is the mint gate's and nothing else's.** `Registry.collision_id` is its
  only lookup, and `entities.birth._refuse_collisions` its only consumer; the knowledge repo's own
  contract linter mirrors the same match key as a declared duplication across two repos with no
  shared import, so a change to `_SUFFIXES` is a two-repo decision. `Registry.canonical_id` — the
  filing side — deliberately does not consult it: which entity a capture MEANS is the agent's
  judgment, fenced by `librarian.gates.resolve_entity_ids` (a declared id must exist) and by the
  park. `server.entity_aliases` uses neither (its own looser `_norm` — a retrieval nicety, not an
  identity decision).
