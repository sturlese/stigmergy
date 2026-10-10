# Audience layers

## 1. Outcome

Readers see one wiki; knowledge is stored in layers. A reader sees the union of the layers they may
read, and pages that share a virtual path are shown as one page. A capture lands in exactly one
layer and everything derived from it stays there. A reader learns nothing about a layer they
cannot read: not its pages, names, counts, links, or errors. Examples use invented names.

## 2. Constraints

**C1. The librarian never sees a layer.** `knowledge/librarian_skill.md` and the planner prompt
hold no instruction, field, or example about layers, and no layer name reaches the prompt. The
librarian works on a view shaped like a repository without layers (`wiki/<role>/<Title>.md`,
`sources/YYYY/MM/<id>.md`, `(Source: \`sources/…\`)`), with every source and envelope shown as if
filed organization-wide. For an all-`public` repository the planner prompt is byte-identical to
the one the tree itself gives. The capture-layer decision (§3.6) is a separate call to a separate
model and shares no text with the librarian.

**C2. No oracle.** Every read surface (fused pages, search, entity listing, backlinks, the graph,
counters, link rendering, errors) answers a reader identically whether or not content exists in
layers the reader cannot read. A write in one layer is never rejected, delayed, or altered because
of the state of a layer its capture cannot read.

**C3. Not provided.** Nested layers, a private-layer flag, per-fact visibility inside a page,
per-identity code paths, a second librarian prompt, a routing confirmation or backoffice routing
control, and moving a source between layers: a misrouted capture is deleted and filed again.

## 3. Model

### 3.1 Layers

- `public` is the root layer: `acl: null`, stored at the repository root, readable by everyone.
- Every other layer is a roster group with a name and a one-line description. Layers are flat.
- Names match `[a-z][a-z0-9-]{1,31}`. `public`, `brain-admins`, `layers`, `wiki`, `sources`, and
  `all` are reserved.
- A layer cannot be deleted while a person (as member or default), a channel, indexed knowledge, or
  a capture still being filed uses it. `brain-admins` is a role, never a layer or an audience;
  workspace administrators (`is_admin`) are people who hold it.

### 3.2 Who reads and who publishes

| | Reads | Publishes to |
|---|---|---|
| Member of a layer | that layer and `public` | that layer |
| Any identity | `public` | `public`, when named explicitly or as its default |
| `brain-admins` role | every layer | only the layers it is a member of, and `public` |

Reading every layer never grants publishing; deleting needs the `brain-admins` role. At filing the
writer checks again that the author may still publish to the layer, or does not file the capture.

### 3.3 Storage and identity

| | `public` | layer `deals` |
|---|---|---|
| Page | `wiki/entities/Acme.md` | `layers/deals/wiki/entities/Acme.md` |
| Source | `sources/2026/09/<id>.md` | `layers/deals/sources/2026/09/<id>.md` |
| `acl` in frontmatter | `null` | `[deals]` |

- The **virtual path** omits the `layers/<layer>/` prefix the **physical path** has; for `public`
  they are equal. A page's identity is (layer, virtual path).
- Page bodies and `sources:` hold virtual paths. Capture ids are unique across layers.
- `acl` holds exactly one layer, or `null`, and must match the folder (the linter checks). Entity
  titles and aliases are unique among the entity pages of one layer.

### 3.4 Write path

**Scope.** A capture filed in layer `L` adds, changes, or deletes files only under `L`'s folder; a
`public` capture touches only root `wiki/` and `sources/`. The diff gate enforces it.

**View.** The librarian is shown a scratch tree with the pages of `public` and `L` under virtual
paths (a `public` capture sees `public` only). A virtual path in both becomes one page whose body
is their merge, which the librarian may update.

**Units.** A body is an H1, then sections separated by headings. A unit is a list item with its
continuation lines, a paragraph, or a contradiction callout. Units are equal when their
whitespace- and punctuation-normalized text is equal.

**Merge** of the `public` body `P` and the layer body `Q`: `P`'s sections in order, each with `P`'s
units followed by `Q`'s units under the same heading, then the sections only `Q` has; one H1.

**Split.** When the plan updates a virtual path that exists in `public`, `L` stores the returned
body minus every unit equal to a unit of `P` under any heading, so a moved unit stays public and
no stale copy stays in `L`. Empty headings are dropped; if nothing remains, nothing is written and
the capture report says so. A capture of another layer never modifies or deletes a `public` page.
The layer page carries `acl: [L]`, its earlier sources plus those the update adds beyond `P`, and
the aliases `P` lacks. Merging `P` with the split of a merged body reproduces it, modulo whitespace.

**Create.** A name claimed by another page of `L` is skipped. A name only `public` claims under the
same virtual path is not a conflict: the page is created in `L`.

**Links.** A wikilink resolves among the pages of the writing layer and `public`; the same stem in
both is one name, and a link that resolves to nothing becomes plain text. Alias collisions are
reported only within a layer; a name claimed by different virtual paths in `public` and `L`
degrades links to plain text for readers of `L` and rejects nothing.

**Other writes.** Contradictions, deletion, gardening, recompiles, and entity merges and deletes
each act inside one layer; renaming an entity renames every page sharing its virtual path in one
commit. Deleting a source also removes, from the pages that remain, the units that cite it.

### 3.5 Read path

- Readers address pages by virtual path; physical paths appear only in backoffice operations.
- **Fused page.** All visible pages with one virtual path are one page; sections merge by heading.
  A section's units are ordered by leading date when every unit has one, keeping the direction its
  first page used; otherwise `public` units come first. Every non-`public` unit except a callout is
  marked `[layer:<name>]`; look-alike markers in stored text are escaped. Connections to one target
  appear once. Aliases, links, and sources are the union of the visible pages'.
- Search ranks physical pages behind the ACL filter and groups results by virtual path with the
  layers that matched. Backlinks and graph edges are the union over visible pages; the graph has
  one node per virtual path. `ask` cites virtual paths.
- MCP responses add `layers`; `read_page`, `describe_entity`, and `list_pages` return fused results.

### 3.6 Choosing the layer of a capture

One resolver (`server/routing.py`) runs where every adapter converges, before queueing. Stigmergy
never asks: a capture that names no layer is routed at submission and the decision reported back.

| Order | Situation | Result |
|---|---|---|
| 1 | The capture names a layer | That layer, if it exists and the caller may publish there; otherwise refused |
| 2 | The adapter fixes the layer (Slack channel) | That layer |
| 3 | The caller has at most one candidate | The default; the classifier is not called |
| 4 | The classifier gives a candidate at least the threshold | That layer |
| 5 | The classifier is unsure, fails, times out, or has no free slot | The default |

- Candidates are the existing layers the caller may publish to, minus `public`, which is never
  chosen automatically.
- A caller without a default layer must name one. A capture that gives the classifier nothing to
  read takes the default without classifying.
- `audience` is a list with exactly one name (a string is refused; `["public"]` is valid). The
  backoffice's layer selector is preselected with the default and classifies nothing.
- Every capture records its decision (`explicit`, `channel`, `default`, or `classified`) and the
  classifier's probabilities when it ran.

#### 3.6.1 The classifier

`typesafe/jev-1.13` (`kernel.llm.LAYER_MODEL`) through OpenRouter's decisions API via `llm.choose`:
a typed-decision model on the closed allowlist (invariant 10) that returns one probability per
option and no text.

- **Options**: each candidate layer with its roster description (its name when it has none), plus
  `no_single_layer` ("none of these layers clearly fits the capture, or it fits more than one of
  them equally well"). Descriptions are the only routing rules; administrators edit them in the
  backoffice.
- **State**: title, file names, address without capability parameters, and the first 8,000
  characters of text readable without a parser (text and Markdown sent by value or through the
  bridge, verified against their digest first). Other files and web pages are routed by title,
  file name, and address only.
- **Choice**: the most probable candidate at or above `THRESHOLD` (0.75); otherwise the default,
  with the probabilities recorded. An answer whose options do not match is a failure.
- **Untrusted input**: the state is data. The classifier chooses only among layers the caller
  could name itself, never `public`, so a capture cannot widen its own audience.
- **Privacy**: the data policy of every model call (no training, zero retention); own text only.

#### 3.6.2 What the caller receives

`brain_submit` returns `layer`, `layer_decision`, and, when the classifier ran, `probabilities`
(`{option: probability}` over the candidates and `no_single_layer`). `brain_submissions` returns
`layer` and `layer_decision` per row. The backoffice capture detail shows the same with the odds,
and the captures list filters to `classified`. Slack's acknowledgement names the layer.

#### 3.6.3 Bounds

One request, a 10-second total timeout, no retry, no model fallback, and at most two requests in
flight per process, so a slow provider cannot hold the server's shared blocking workers; with no
slot free the capture takes the default at once. Failures are logged by class only. Only the
documented response fields are accepted. Tests replace the client with a scripted decider.

#### 3.6.4 Calibration

- **Case table** (keyless): the LAY-14 table drives rows 1–5 with a scripted decider, including a
  layer the caller may not publish to, one-candidate callers, probabilities at and below the
  threshold, a timeout, and an API error.
- **Calibration** (paid): `evals/run_routing.py` runs the invented captures of
  `evals/routing_golden.json` against the real classifier. The bar: every unambiguous case lands in
  its expected layer and every deliberately ambiguous one in the default.
- **Known limit**: a capture that fits two of the sender's layers about equally can land, with high
  probability, in the one whose description names its topic instead of the default. The stored
  probabilities are the running record, reviewed in the backoffice.

## 4. Surfaces

| Surface | Layers |
|---|---|
| Backoffice, access | Layers with descriptions; memberships; mandatory default layer; one layer per Slack channel |
| Backoffice, captures | Layer and decision in list and detail; `classified` filter; layer selector limited to what the administrator may publish to |
| Backoffice, entities | Grouped by virtual path with their layers; rename across layers; merge and delete inside one |
| Backoffice, changes and graph measures | Physical paths; measures per scope (whole brain, `public`, each layer) |
| Wiki and graph | Fused pages with layer badges; one graph node per virtual path; a layer filter |
| Slack | One layer per channel; the acknowledgement names it |

## 5. Acceptance criteria

1. **LAY-01** A capture in layer `L` changes files only under `L`'s folder.
2. **LAY-02** The same title can exist as a page in several layers; neither blocks the other, in
   either order of arrival.
3. **LAY-03** A `public` capture that mentions a name existing only in another layer creates its
   own `public` page and reveals nothing.
4. **LAY-04** A capture in `L` that adds facts about a `public` entity stores them in `L`; the
   `public` page is unchanged.
5. **LAY-05** A reader of `public` and `L` reads one fused page; a reader of `public` reads the
   `public` part; a reader of another layer sees nothing of `L`.
6. **LAY-06** Every read surface returns byte-identical responses to a scoped reader with and
   without content in layers they cannot read.
7. **LAY-07** An identity cannot publish to a layer it is not a member of, including identities
   with the `brain-admins` role.
8. **LAY-08** An identity without a default layer cannot submit without naming one.
9. **LAY-09** Merge and split round-trip; existing cited units survive every update.
10. **LAY-10** The planner prompt for a `public`-only repository is the one the tree itself gives.
11. **LAY-11** No layer name reaches the planner prompt in any case.
12. **LAY-12** Counters shown to non-administrators count only what they can read.
13. **LAY-13** Deleting a source leaves no unit citing it.
14. **LAY-14** The resolver table of §3.6 holds row by row, driven by a case table.
15. **LAY-15** The classifier never selects `public` or a layer the caller may not publish to; below
    the threshold, on failure, or on timeout the capture lands in the default within the timeout.
16. **LAY-16** Every routed capture reports its layer and how it was decided to the caller and in
    the backoffice, with probabilities when the classifier ran.
17. **LAY-17** The keyless suite never calls the classifier's API.
