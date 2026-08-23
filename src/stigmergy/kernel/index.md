# kernel — the shared bottom of the stack

A LIBRARY, not a layer: importable from anywhere, and it imports nothing from this project except
itself. `stigmergy.text` is a sibling at the bottom of the stack, not part of it. The layering, the no-SDK-at-module-level rule and each consumer's declared reach are
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
| `acl.py` | The audience VOCABULARY — what two label lists mean when they meet, never who is asking. `flows_into(content_acl, page_acl)` — may this content be written into, or rendered onto, a page with that label? Containment, fail-closed, and the whole of this module. Where a label COMES FROM is not here: the door decides it and the capture's queue row carries it |
| `registry.py` | `Registry`, `load_registry` / `registry_from_text` / `save_registry` / `index_entity` — `ops/entity-registry.json`'s one reader/writer, plus `title` / `type_of` and the TWO lookups the registry is asked for: `canonical_id` (which entity does this text MEAN — filing) and `collision_id` (would this new name be confused with one we have — the mint gate). Missing file = empty registry; malformed = loud error. The reader is split path-from-text because the registry also reaches a reader as BYTES now (the index's snapshot, which `index.check` lints through this same parse) |
| `normalize.py` | `resolution_key(name)` (accents, case and punctuation folded — and nothing that is a judgment), `normalize(name)` (that plus the legal-suffix table: the COLLISION key), `slugify(s)` (≤60 chars) |
| `fsutil.py` | `write_text_atomic(path, text)` — tmp file + same-directory `os.replace`, so a concurrent reader never sees a partial |

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
- `acl.flows_into` for every seam where a model reads one governed page while writing another,
  and for a view's member and backlink feeds — never a hand-rolled label comparison. It is
  containment, not intersection, and getting that backwards leaks to the rest of an audience.
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
- Loading a provider SDK at module level: `llm.build_model` imports its own inside the function
  body, so a keyless offline run pays for nothing.
- Reading the environment anywhere but at call time.
- "Unifying" `acl` with `stigmergy.server.acl.visible` — the server's is a deliberately STRICTER
  separate implementation (a malformed stored value hides from everyone) that mirrors this module
  rather than importing it.
- Letting a consumer re-derive a registry, ACL or normalization rule instead of importing it.

## Contracts

- ACL truth table: `flows_into` — open content flows anywhere; nothing labelled flows into an
  open page; otherwise every group of the PAGE must be a group of the CONTENT. `view_acl` was here too, computing a view's own label as
  the intersection of its members'; it was retired, because the intersection collapsed a
  view to nobody the moment two members disagreed. One dialect throughout: `None` is open, `[]` is nobody, and
  nothing here collapses one into the other.
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
