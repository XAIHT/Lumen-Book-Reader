# Lumen Book Reader MCP — external-client specification

> **Specification version:** 1.0<br>
> **Server:** Lumen Book Reader 1.7.0<br>
> **MCP SDK validated:** 2.1.1<br>
> **Primary external client:** Tlamatini<br>
> **Transport in `LumenBookReader.json`:** MCP over STDIO<br>
> **Public tools:** 7<br>
> **Public resources:** 5<br>
> **Public prompts:** 3<br>
> **Tool profile:** read-only, non-destructive, idempotent, closed-world

This is the implementation reference for the MCP server in this repository. It
describes the callable surface that an external assistant receives after loading
`LumenBookReader.json`. Parameter names, defaults, limits, result fields,
fallbacks, resource URIs, and errors are taken from the running implementation.

---

## 1. Tlamatini installation descriptor

The ready-to-import descriptor is:

```text
C:\Lumen-Book-Reader\LumenBookReader.json
```

Its complete content is:

```json
{
  "mcpServers": {
    "lumen-book-reader": {
      "command": "C:\\Lumen-Book-Reader\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "lumen_reader.mcp_server",
        "serve",
        "--stdio"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

The wrapper, command/argument arrays, and environment layout match the supplied
Onion Search external-MCP descriptor. No shell parses the arguments. The
dedicated interpreter contains MCP SDK 2.1.1 and an editable installation of
this exact checkout, so Tlamatini does not depend on its own current directory.

### 1.1 Manual launch tests

```powershell
# Non-mutating runtime diagnosis
& "C:\Lumen-Book-Reader\.venv\Scripts\python.exe" `
  -m lumen_reader.mcp_server doctor --json

# MCP STDIO server; an MCP client normally owns this process
& "C:\Lumen-Book-Reader\.venv\Scripts\python.exe" `
  -m lumen_reader.mcp_server serve --stdio
```

In STDIO mode, standard output is reserved for MCP frames. Diagnostics and
warnings go to standard error.

---

## 2. Server identity and protocol contract

| Property | Value |
|---|---|
| MCP server name | `Lumen Book Reader` |
| Application version | `1.7.0` |
| Default transport | STDIO |
| Optional local transport | Streamable HTTP |
| Local HTTP endpoint | `http://127.0.0.1:8765/mcp` |
| Wire encoding | UTF-8 MCP messages |
| Tool result representation | Structured content plus JSON text representation |
| Database access from tools | SQLite read-only/query-only connections |
| Default connection limit | 8 concurrent database readers |
| Arbitrary caller filesystem paths | Never accepted by public tools |
| Returned source URIs | `lumen://...` only |
| Tool catalog mutation during a session | None |

All seven tools advertise these MCP annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Book passages are untrusted source material. They are returned as evidence, not
as server instructions, policy, configuration, or executable input.

---

## 3. Complete tool catalog

| # | Tool | Primary use | Required parameters | Default result limit |
|---:|---|---|---|---:|
| 1 | `lumen_status` | Discover roots, coverage, limits, and active backends | None | N/A |
| 2 | `lumen_glob` | Find books/sections by relative path or metadata glob | `pattern` | 50 |
| 3 | `lumen_grep` | Find literal, phrase, FTS, or regex text matches | `query` | 30 |
| 4 | `lumen_search` | Rank topical and semantically expanded passages | `query` | 20 |
| 5 | `lumen_related` | Retrieve adjacent, conceptual, author, subject, or contrasting evidence | Exactly one seed | 20 |
| 6 | `lumen_get_book` | Read one book's metadata, coverage, TOC, and passage links | `book_id` | One book |
| 7 | `lumen_explain_query` | Explain a bounded plan without running the full content query | `operation`, `query` | One plan |

Recommended assistant order:

1. Call `lumen_status` once to learn root IDs and coverage.
2. Use `lumen_glob` when identifying files/books by name or metadata.
3. Use `lumen_grep` when exact wording or a regex matters.
4. Use `lumen_search` for a topic or question.
5. Use `lumen_related` to expand from a strong seed.
6. Resolve only the most useful returned `lumen://` resources.
7. Preserve each hit's book and locator fields in citations.

---

## 4. Shared parameter types and limits

### 4.1 Root IDs

Tools accept opaque root IDs, not directory paths. Obtain them from
`lumen_status.roots[].root_id`.

```json
{
  "roots": ["root_02c4b349158d66de"]
}
```

An omitted, `null`, or empty root list means every root already authorized and
indexed by Lumen. It never means every drive on the computer. Unknown IDs fail
with `ROOT_NOT_AUTHORIZED`.

### 4.2 Book IDs

`book_ids` contains 1–500 positive integers returned by Lumen results. Zero,
negative IDs, or more than 500 IDs fail with `INVALID_ARGUMENT`.

### 4.3 Formats

Accepted values are `epub`, `.epub`, `pdf`, and `.pdf`, case-insensitively.
They normalize to `.epub` and `.pdf`. Any other format fails with
`INVALID_ARGUMENT`.

### 4.4 Limits

| Limit | Effective range/value |
|---|---:|
| Tool results | 1–100 |
| Search excerpt | 120–4,000 Unicode characters |
| Grep context | 80–2,000 Unicode characters |
| Grep matches per book | 1–20 |
| Regex pattern length | 1–2,048 characters |
| Other text query length | 1–4,096 characters |
| Related free-text seed retained | 16,384 characters |
| Regex candidates | 20,000 maximum |
| Regex inspected text | 4 MiB maximum per request |
| Regex verification deadline | 50 ms per passage |
| Passage/section resource body | 65,536 UTF-8 bytes/characters, bounded by resolver |
| Adjacent passage expansion | At most 5 before and 5 after through the internal resolver |
| Metadata relationship candidates | 500 books |
| Cursor encoded size | At most 4,096 characters |
| Cursor lifetime | 3,600 seconds |
| SQLite query connections | 8 by default, hard-clamped to 1–32 |

Integer result limits outside the public range are clamped to 1–100.

### 4.5 Cursors

Cursors are opaque HMAC-signed continuation tokens. A cursor is bound to:

- the tool operation;
- normalized query/filter digest;
- active corpus revision;
- authorized root-set digest;
- continuation offset;
- issue and expiry times.

Never edit or decode one in a client. Reuse it only with the identical request.
Changed queries, roots, or corpus revisions produce explicit cursor errors.

### 4.6 Citation IDs

Passage hits contain signed IDs beginning with `lumencite:v1:`. They bind:

- book ID;
- passage ID;
- passage revision;
- the first 16 hexadecimal characters of the passage SHA-256;
- an HMAC signature using the per-user Lumen MCP citation key.

They contain no book text or filesystem path.

---

## 5. `lumen_status`

### 5.1 Purpose

Reports server health, corpus/index state, configured roots, passage coverage,
hard limits, and the retrieval/hardware backends actually available.

### 5.2 Parameters

| Parameter | Type | Required | Default | Meaning |
|---|---|---:|---:|---|
| `include_roots` | boolean | No | `true` | Include authorized root IDs, paths, existence, and book counts. |
| `include_backends` | boolean | No | `true` | Include lexical, regex, offline semantic, hardware, and probe state. |
| `include_recent_failures` | boolean | No | `false` | Include up to ten recent non-successful scan records when the legacy table supports them. |

### 5.3 Example request

```json
{
  "include_roots": true,
  "include_backends": true,
  "include_recent_failures": false
}
```

### 5.4 Ready result fields

| Field | Meaning |
|---|---|
| `schema_version` | MCP result-contract version, currently `1.0`. |
| `operation` | `lumen_status`. |
| `request_id` | Unique hexadecimal request correlation ID. |
| `server.version` | Lumen application version. |
| `server.process_id` | MCP process ID. |
| `server.python` | Runtime Python version. |
| `server.uptime_seconds` | Process-relative uptime. |
| `server.transport_default` | `stdio`. |
| `server.read_only` | Always `true` for this public profile. |
| `catalog.path` | Resolved Lumen index path. |
| `catalog.exists` | Whether the index currently exists. |
| `catalog.bytes` | Current database byte size. |
| `catalog.journal_mode` | SQLite journal mode observed by the query connection. |
| `catalog.query_only` | Always `true`. |
| `catalog.passage_schema_version` | `1` when passage resources exist, otherwise `0`. |
| `roots[]` | Authorized root records. |
| `corpus.corpus_revision` | Active passage-corpus revision counter. |
| `corpus.books` | Cataloged book count. |
| `corpus.passages` | Active passage count. |
| `corpus.coverage` | Counts grouped by coverage state. |
| `corpus.passage_index` | `ready` or `legacy_fallback`; schema existence alone does not count as built passage content. |
| `backends.lexical` | SQLite FTS5 availability/selection. |
| `backends.regex` | Timeout-capable regex availability/selection. |
| `backends.semantic` | Offline WordNet semantic-expansion status and model identity. |
| `backends.hardware[]` | Detected accelerator facts, registration, and actual-use flags. |
| `backends.probe_state` | `running` or `complete`. |
| `limits` | Server-enforced result/excerpt/regex/connection bounds. |
| `health` | `ready` for a queryable index. |
| `warnings[]` | Human-readable bounded conditions. |

If no index exists, `health` is `not_indexed`, corpus counts are zero, and the
result instructs the user to configure and sweep Lumen.

---

## 6. `lumen_glob`

### 6.1 Purpose

Matches books using glob syntax over indexed relative paths or metadata. It
does not walk a caller-supplied directory and cannot escape Lumen's roots.

### 6.2 Parameters

| Parameter | Type | Required | Default | Allowed/effective values |
|---|---|---:|---|---|
| `pattern` | string | **Yes** | — | Relative glob, 1–2,048 characters. |
| `target` | string | No | `path` | `path`, `filename`, `title`, `author`, `subject`, `publisher`, `any_metadata`. |
| `roots` | string[] or null | No | all authorized | Opaque IDs from `lumen_status`. |
| `formats` | string[] or null | No | EPUB + PDF | `epub`, `pdf`, with optional leading period. |
| `case_sensitive` | string | No | `auto` | `auto`, `true`, `false`. Auto is insensitive on Windows and sensitive elsewhere. |
| `include_sections` | boolean | No | `false` | Also match active section titles/hrefs. |
| `sort` | string | No | `path` | `path`, `title`, `modified`, `size`. |
| `limit` | integer | No | `50` | Clamped to 1–100. |
| `cursor` | string or null | No | null | Signed continuation cursor from an identical prior call. |

### 6.3 Glob grammar

| Syntax | Meaning |
|---|---|
| `*` | Zero or more characters except `/`. |
| `**` | Zero or more characters including directory separators. |
| `**/` | Zero or more complete path components. |
| `?` | One character except `/`. |
| `[abc]` | One listed character. |
| `[!abc]` | One character not listed. |

Backslashes normalize to `/`. Absolute patterns, drive-prefixed patterns, UNC
patterns, and any `..` path component fail with `INVALID_ARGUMENT`.

### 6.4 Example requests

```json
{"pattern":"**/*radio*.pdf","target":"path","limit":25}
```

```json
{
  "pattern":"*Mourinho*",
  "target":"any_metadata",
  "formats":["epub"],
  "include_sections":true
}
```

### 6.5 Book-hit fields

`rank`, `resource_uri`, `book_id`, `root_id`, `relative_path`, `path`, `name`,
`title`, `author`, `format`, `language`, `subjects`, `publisher`, `size_bytes`,
`modified_ns`, `coverage`, and `matched_value`.

A section hit additionally has `match_kind: "section"` and:

```json
{
  "section": {
    "ordinal": 4,
    "title": "Receiver architecture",
    "href": "text/chapter-4.xhtml"
  }
}
```

### 6.6 Result envelope

`schema_version`, `operation`, `request_id`, `corpus_revision`, `backend`,
`partial`, `warnings`, `timing.total_ms`, `hits`, and `next_cursor`.

The backend reports `sqlite-catalog` plus `glob-verifier`. When section coverage
has not been built, book matching still works and `warnings` explains that
section matching was omitted.

---

## 7. `lumen_grep`

### 7.1 Purpose

Locates exact or pattern-based text across active passages, returning verified
match ranges and precise page/section locators.

### 7.2 Parameters

| Parameter | Type | Required | Default | Allowed/effective values |
|---|---|---:|---|---|
| `query` | string | **Yes** | — | 1–4,096 chars; regex maximum 2,048. |
| `mode` | string | No | `literal` | `literal`, `phrase`, `fts`, `regex`. |
| `case_sensitive` | boolean | No | `false` | Exact/regex matching case policy. FTS cannot be case-sensitive. |
| `whole_word` | boolean | No | `false` | Requires Unicode alphanumeric/underscore boundaries for literal/phrase verification. |
| `roots` | string[] or null | No | all authorized | Root IDs from status. |
| `book_ids` | integer[] or null | No | all books | 1–500 positive IDs. |
| `formats` | string[] or null | No | EPUB + PDF | `epub` and/or `pdf`. |
| `max_matches_per_book` | integer | No | `3` | Clamped to 1–20. |
| `context_chars` | integer | No | `480` | Clamped to 80–2,000. |
| `fallback` | string | No | `none` | `none`, `literal`, `fts`; used only when the requested backend is unavailable. |
| `limit` | integer | No | `30` | Clamped to 1–100. |
| `cursor` | string or null | No | null | Reserved continuation input; current grep result returns null. |

### 7.3 Modes

| Mode | Candidate generation | Verification | Match ranges |
|---|---|---|---|
| `literal` | Safe FTS candidates when possible | Exact substring scan | Yes |
| `phrase` | FTS phrase candidate | Exact phrase scan | Yes |
| `fts` | SQLite FTS5 expression | FTS result is authoritative | Empty; `matches_in_passage` is null |
| `regex` | Required literal or explicit book scope | Timeout-capable `regex` engine | Yes |

A regex without an indexable literal of at least three word characters must
include explicit `book_ids`; otherwise the call fails with `REGEX_TOO_BROAD`.
Each passage receives a 50 ms regex deadline. Total inspected text is capped at
4 MiB and the candidate set at 20,000.

### 7.4 Example requests

```json
{
  "query":"frequency hopping",
  "mode":"phrase",
  "case_sensitive":false,
  "whole_word":false,
  "context_chars":600
}
```

```json
{
  "query":"freq(?:uency)?\\s+hopp(?:ing|ed)",
  "mode":"regex",
  "book_ids":[121,908],
  "max_matches_per_book":5,
  "fallback":"none"
}
```

### 7.5 Grep-specific result fields

Each hit uses the common passage-hit shape and adds:

| Field | Meaning |
|---|---|
| `match_ranges[]` | Half-open `{start,end}` offsets within `excerpt`. |
| `matches_in_passage` | Number of verified ranges considered in that passage; null for FTS mode. |

The envelope's `partial` becomes true if the inspected-text budget is reached.
On a legacy catalog, grep uses capped book-head text, returns book-level
precision, and warns that exact locations are limited.

---

## 8. `lumen_search`

### 8.1 Purpose

Ranks topical passages using SQLite FTS5. For `auto`, `hybrid`, and `semantic`
strategies, the server performs bounded offline Princeton WordNet 3.0 semantic
query expansion and executes the expanded expression against the local index.
No network lookup or remote model is used.

### 8.2 Parameters

| Parameter | Type | Required | Default | Allowed/effective values |
|---|---|---:|---|---|
| `query` | string | **Yes** | — | Human query, 1–4,096 characters. |
| `strategy` | string | No | `auto` | `auto`, `lexical`, `hybrid`, `semantic`. |
| `roots` | string[] or null | No | all authorized | Root IDs from status. |
| `formats` | string[] or null | No | EPUB + PDF | `epub` and/or `pdf`. |
| `languages` | string[] or null | No | all | Case-folded metadata codes/names, each capped at 32 chars. |
| `book_ids` | integer[] or null | No | all | 1–500 positive IDs. |
| `diversity` | string | No | `book` | `book` enforces `max_per_book`; `none` disables that diversity cap. |
| `max_per_book` | integer | No | `3` | Clamped to 1–20 when diversity is `book`. |
| `include_adjacent` | boolean | No | `false` | Adds an adjacent-context URI hint to passage hits. |
| `coverage` | string | No | `include_partial` | `include_partial` or `complete_only`. |
| `limit` | integer | No | `20` | Clamped to 1–100. |
| `excerpt_chars` | integer | No | `700` | Clamped to 120–4,000. |
| `cursor` | string or null | No | null | Signed continuation cursor from an identical request. |

### 8.3 Strategy behavior

| Strategy | Execution |
|---|---|
| `lexical` | Safe query normalization followed by passage FTS5. |
| `semantic` | Original expression OR bounded offline WordNet lemma/phrase expansions, then FTS5 ranking. |
| `hybrid` | Same offline semantic expansion with lexical matching retained as the base expression. |
| `auto` | Selects the complete offline lexical+WordNet path when WordNet is healthy. |

`backend.used` identifies both the passage/book-head index and
`wordnet-query-expansion` when expansion contributes. `backend.model_id` is
`Princeton WordNet 3.0` for expanded calls.

### 8.4 Example request

```json
{
  "query":"spread spectrum interference resistance",
  "strategy":"semantic",
  "formats":["pdf","epub"],
  "diversity":"book",
  "max_per_book":2,
  "include_adjacent":true,
  "coverage":"include_partial",
  "limit":12,
  "excerpt_chars":900
}
```

### 8.5 Result envelope

`schema_version`, `operation`, `request_id`, `corpus_revision`, `backend`,
`coverage`, `partial`, `warnings`, `timing.total_ms`, `hits`, and `next_cursor`.

`coverage` contains:

| Field | Meaning |
|---|---|
| `documents_in_scope` | Books remaining after root filters. |
| `documents_complete` | Books with complete passage extraction. |
| `documents_partial` | Capped/unbuilt/non-complete documents. |
| `passages_in_scope` | Active passage count. |
| `is_complete_for_scope` | True only if the scope is non-empty and every document is complete. |

If no active passage matches, the server searches the existing capped
`content_fts` book-head tier. Such hits have `precision: "book_level"`, a book
resource URI, no signed passage citation, and an explicit coverage warning.

---

## 9. `lumen_related`

### 9.1 Purpose

Expands from exactly one passage, book, citation, or free-text seed.

### 9.2 Parameters

| Parameter | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `passage_id` | integer or null | Conditional | null | Active passage seed. |
| `book_id` | integer or null | Conditional | null | Indexed book seed. |
| `citation_id` | string or null | Conditional | null | Signed `lumencite:v1:` seed. |
| `text` | string or null | Conditional | null | Free-text conceptual seed, capped at 16,384 chars. |
| `relationship` | string | No | `conceptual` | `adjacent`, `conceptual`, `same_subject`, `same_author`, `contrasting`. |
| `exclude_same_book` | boolean | No | `false` | Remove hits from the seed book. |
| `strategy` | string | No | `auto` | Same strategies as `lumen_search`. |
| `limit` | integer | No | `20` | Clamped to 1–100. |

Exactly one of `passage_id`, `book_id`, `citation_id`, or non-empty `text` must
be supplied. Zero or multiple seeds fail with `INVALID_ARGUMENT`.

### 9.3 Relationships

| Relationship | Valid seeds | Exact behavior |
|---|---|---|
| `adjacent` | `passage_id`, `citation_id` | Previous/next active passages in the same revision and source order. A book/text seed is rejected because it has no exact passage position. |
| `conceptual` | Any seed | Builds a bounded lexical signature from seed subjects/title/section/body/text and searches with the selected strategy. |
| `same_author` | Passage, citation, book | Requires usable author metadata, filters candidates by exact normalized author identity, then ranks their passages. |
| `same_subject` | Passage, citation, book | Requires subject metadata, filters candidates by normalized subject-label overlap, then searches with a server-owned OR across seed subject labels. |
| `contrasting` | Any seed | Retrieves possible qualifying/counterevidence candidates and warns that retrieval does not prove logical contradiction. |

Author/subject candidate lists are capped at 500 books. Legacy catalogs still
return metadata-filtered, capped book-level evidence with explicit precision.

### 9.4 Example requests

```json
{
  "citation_id":"lumencite:v1:eyJiIjoxMj...",
  "relationship":"adjacent",
  "limit":4
}
```

```json
{
  "book_id":731,
  "relationship":"same_author",
  "exclude_same_book":true,
  "strategy":"semantic",
  "limit":10
}
```

```json
{
  "text":"Evidence that frequency hopping resists narrowband interference",
  "relationship":"conceptual",
  "strategy":"hybrid"
}
```

### 9.5 Result details

The result uses the search envelope plus `relationship`. Metadata relationships
append `metadata-author-identity` or `metadata-subject-overlap` to both
`backend.used` and each hit's `contributors`. Adjacent results report
`passage-adjacency`.

---

## 10. `lumen_get_book`

### 10.1 Purpose

Returns the canonical indexed metadata and coverage for one book, plus bounded
TOC and representative-passage links when requested.

### 10.2 Parameters

| Parameter | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `book_id` | integer | **Yes** | — | Positive indexed book ID. |
| `include_toc` | boolean | No | `true` | Include active revision sections in source order. |
| `include_coverage` | boolean | No | `true` | Include coverage state/counts/reason/revision/time. |
| `include_representative_passages` | boolean | No | `false` | Include up to five active passage resource URIs. |

### 10.3 Example request

```json
{
  "book_id":731,
  "include_toc":true,
  "include_coverage":true,
  "include_representative_passages":true
}
```

### 10.4 Result fields

| Field | Meaning |
|---|---|
| `schema_version` | Result schema version. |
| `operation` | `lumen_get_book`. |
| `request_id` | Request correlation ID. |
| `book` | Complete safe book metadata object. |
| `resource_uri` | `lumen://book/{book_id}`. |
| `can_open_in_lumen` | Currently `false`; no application-launch side effect. |
| `coverage.status` | Passage coverage state. |
| `coverage.reason` | Bounded explanation. |
| `coverage.active_revision` | Active passage revision or null. |
| `coverage.sections` | Active section count. |
| `coverage.passages` | Active passage count. |
| `coverage.characters` | Active indexed character count. |
| `coverage.indexed_at` | Activation timestamp. |
| `toc[]` | Ordered sections with locator fields and section resource URI. |
| `representative_passages[]` | Up to five revision-bound passage URIs. |

Unknown IDs fail with `BOOK_NOT_FOUND`. On a pre-passage database, book
metadata remains available and coverage is `metadata_only`.

---

## 11. `lumen_explain_query`

### 11.1 Purpose

Validates and explains a proposed glob/grep/search plan without executing the
full corpus content query. It never returns SQL.

### 11.2 Parameters

| Parameter | Type | Required | Default | Meaning |
|---|---|---:|---|---|
| `operation` | string | **Yes** | — | `glob`, `grep`, or `search`. |
| `query` | string | **Yes** | — | Proposed pattern/query. |
| `strategy` | string | No | `auto` | Search backend strategy; ignored for glob. |

### 11.3 Example request

```json
{
  "operation":"search",
  "query":"modern radio receiver architecture",
  "strategy":"semantic"
}
```

### 11.4 Result fields

`schema_version`, `operation`, `request_id`, `plan`, and `limits`.

For glob, `plan` includes `fixed_prefix`, candidate scope, and backend. For
grep/search, it includes the normalized safe FTS expression, candidate scope,
requested/used backend, fallback reason, and warnings. An unsupported operation
returns a rejected plan with a supported-operation warning.

---

## 12. Common passage/search hit schema

Passage hits returned by search, grep, and related operations use:

```json
{
  "rank": 1,
  "score": 0.82,
  "score_kind": "fts5_bm25_inverse",
  "contributors": ["sqlite-fts5", "wordnet-query-expansion"],
  "citation_id": "lumencite:v1:...",
  "resource_uri": "lumen://passage/4501?revision=7",
  "book": {
    "id": 731,
    "title": "Modern Radio Frequency Technologies",
    "authors": ["A. Writer"],
    "author": "A. Writer",
    "format": "epub",
    "language": "en",
    "publisher": "Example Press",
    "subjects": "Radio, Engineering",
    "description": "...",
    "path": "C:\\books\\radio.epub",
    "name": "radio.epub",
    "size_bytes": 1700000,
    "modified_ns": 1780000000000000000,
    "pages": 410,
    "readable": true,
    "error": ""
  },
  "locator": {
    "kind": "epub_spine",
    "section_ordinal": 4,
    "section_title": "Receiver architecture",
    "href": "text/chapter-4.xhtml",
    "fragment": "",
    "page_start": null,
    "page_end": null,
    "passage_ordinal": 37,
    "char_start": 12000,
    "char_end": 13750
  },
  "excerpt": "...",
  "match_ranges": [],
  "passage_sha256": "...",
  "coverage": "complete",
  "precision": "passage",
  "modified_at_ns": 1780000000000000000
}
```

### 12.1 Score semantics

The current score is `1 / (1 + abs(raw_bm25_rank))`. It is suitable for
ordering within one response; it is not a calibrated probability and should
not be compared across unrelated queries or corpus revisions.

### 12.2 Locator semantics

- PDF locations use one-based `page_start`/`page_end`.
- EPUB locations use spine `section_ordinal`, `section_title`, and `href`.
- Passage character ranges are half-open offsets in the normalized section.
- Grep `match_ranges` are half-open offsets relative to the returned excerpt.

### 12.3 Book-level fallback hit

When only the capped legacy book-head tier is available:

- `citation_id` is null;
- `resource_uri` is `lumen://book/{id}`;
- locator kind is `book_head`;
- precision is `book_level`;
- coverage is `capped`;
- contributors contain `sqlite-fts5-book-head`.

---

## 13. Coverage states

| State | Meaning | Retrieval precision |
|---|---|---|
| `complete` | Complete EPUB spine/PDF page extraction successfully activated. | Passage plus exact page/section locators. |
| `capped` | Ordinary sweep's bounded full-text row is being used as the fast fallback. | Book-level evidence within the extraction cap; exact passage citations require the separate build. |
| `metadata_only` | Catalog metadata exists without active passage content. | Book discovery/metadata only. |
| `no_text_layer` | Source supplied no extractable text. | Metadata and source health only. |
| `locked` | Password-protected source could not be extracted without credentials. | Metadata only. |
| `failed` | Complete revision build failed; prior active revision remains authoritative. | Prior active coverage, when present. |

Coverage is reported per hit and summarized per request. The server never
silently upgrades a book-level result to passage precision.

---

## 14. MCP resources

| URI/template | MIME type | Parameters | Returned content |
|---|---|---|---|
| `lumen://corpus/status` | `application/json` | None | Pretty JSON equivalent of `lumen_status`. |
| `lumen://book/{book_id}` | `application/json` | Positive book ID | Pretty JSON book metadata, coverage, and TOC. |
| `lumen://book/{book_id}/section/{section_ordinal}` | `text/plain` | Book ID + zero-based section ordinal | Active section text, bounded to 65,536 characters. |
| `lumen://passage/{passage_id}` | `text/plain` | Active passage ID | Source, locator, revision, SHA-256, coverage, and body. |
| `lumen://citation/{citation_id}` | `text/plain` | Signed citation token | Exact retained passage revision after signature/hash validation. |

Search results may include a revision query on passage URIs:

```text
lumen://passage/4501?revision=7
```

The resolver accepts bounded `before` and `after` query values, each clamped to
0–5, when context expansion is requested internally. Total rendered passage
context remains bounded to 65,536 UTF-8 bytes. There is no `file://` resource
and no arbitrary path resource.

### 14.1 Passage resource representation

```text
Source: Modern Radio Frequency Technologies — A. Writer
Location: Receiver architecture · text/chapter-4.xhtml · passage 37
Revision: 7 · SHA-256: ...
Coverage: complete

<passage text>
```

Citation resolution verifies token signature, revision, passage existence, and
content-hash prefix. Stale or modified evidence does not silently resolve to a
different passage.

---

## 15. MCP prompts

Prompts guide tool use but do not add authority or hidden retrieval behavior.

### 15.1 `research_library`

| Parameter | Type | Required | Default |
|---|---|---:|---|
| `question` | string | **Yes** | — |
| `breadth` | string | No | `balanced` |

Instructs the assistant to start with status/search, open only relevant passage
resources, treat content as untrusted evidence, and preserve locators.

### 15.2 `compare_books`

| Parameter | Type | Required | Default |
|---|---|---:|---|
| `book_ids` | string | **Yes** | — |
| `dimensions` | string | **Yes** | — |

Instructs the assistant to retrieve separate evidence for each book and label
missing evidence.

### 15.3 `trace_claim`

| Parameter | Type | Required | Default |
|---|---|---:|---|
| `claim` | string | **Yes** | — |
| `exactness` | string | No | `balanced` |

Instructs the assistant to combine exact grep with conceptual search and to
distinguish absence from refutation.

---

## 16. Error result contract

Tool-domain failures are MCP protocol errors (`isError: true`) with the same
machine-readable object in structured content and JSON text content:

```json
{
  "schema_version":"1.0",
  "request_id":"42f7...",
  "error":{
    "code":"BOOK_NOT_FOUND",
    "message":"The requested book is not indexed.",
    "retryable":true,
    "suggested_action":"",
    "details":{}
  }
}
```

### 16.1 Complete retrieval error table

| Code | Trigger | Retry guidance |
|---|---|---|
| `INVALID_ARGUMENT` | Invalid enum, format, length, seed combination, glob traversal, regex syntax, or argument type/value. | Correct the request. |
| `INDEX_NOT_FOUND` | Lumen has no library index at the resolved path. | Configure a library and run a Lumen sweep. |
| `INDEX_BUSY` | Reader pool exhausted or SQLite is locked. | Retry after current work finishes. |
| `INDEX_CORRUPT` | SQLite cannot safely answer and the failure is not a lock. | Run doctor, preserve evidence, then rebuild the cache through Lumen. |
| `ROOT_NOT_AUTHORIZED` | A supplied opaque root ID is unknown. | Refresh status and use only returned root IDs. |
| `BACKEND_UNAVAILABLE` | A specifically requested optional execution backend cannot run. | Select the reported operational strategy or install the required package. |
| `REGEX_TOO_BROAD` | Regex lacks an indexable literal and explicit book scope. | Add a literal or `book_ids`. |
| `QUERY_TIMEOUT` | Regex exceeds its per-passage deadline. | Narrow expression/root/book/format scope. |
| `PASSAGE_INDEX_UNAVAILABLE` | Passage/citation/section operation targets a legacy catalog. | Run a sweep and complete passage build. |
| `BOOK_NOT_FOUND` | Book or section ID does not resolve. | Rediscover through glob/search. |
| `METADATA_UNAVAILABLE` | Same-author/subject relation lacks usable seed metadata. | Select another relationship/seed. |
| `STALE_RESOURCE` | Passage revision/hash is absent, stale, or superseded. | Repeat discovery/search. |
| `INVALID_CITATION` | Citation prefix, payload, version, or HMAC is invalid. | Use an untouched citation from the current server. |
| `INVALID_CURSOR` | Cursor is malformed, oversized, signed by another server, or belongs to another query. | Restart pagination. |
| `CURSOR_EXPIRED` | Cursor is older than one hour. | Repeat the original query. |
| `CURSOR_STALE` | Corpus revision changed after cursor issue. | Repeat the original query. |
| `CURSOR_SCOPE_MISMATCH` | Root authorization scope differs from cursor scope. | Restart within the current scope. |

MCP/Pydantic schema-validation errors can occur before the tool function runs;
those are also returned by the SDK as `isError: true`.

---

## 17. Runtime path discovery

The external descriptor contains no library root or index path. The sidecar
discovers Lumen's own per-user state:

| Platform | Default data directory |
|---|---|
| Windows | `%APPDATA%\Lumen Reader\Lumen` |
| macOS | `~/Library/Application Support/Lumen Reader/Lumen` |
| Linux | `$XDG_DATA_HOME/Lumen Reader/Lumen`, otherwise `~/.local/share/Lumen Reader/Lumen` |

Files beneath the data directory:

| File/directory | Purpose |
|---|---|
| `library-index.db` | Catalog, legacy FTS, and revisioned passages. |
| `reader-state.json` | Lumen desktop state. |
| `mcp-cache/citation.key` | Per-user citation/cursor HMAC key. |
| `logs/` | Sidecar diagnostic location. |

Test/portable overrides:

| Override | Meaning |
|---|---|
| Global CLI `--data-dir PATH` | Replace the data directory for this process. |
| Global CLI `--index PATH` | Replace only the index path. |
| `LUMEN_DATA_DIR` environment variable | Environment equivalent of `--data-dir`. |
| `LUMEN_INDEX_PATH` environment variable | Environment equivalent of `--index`. |

CLI global overrides must appear before the subcommand.

---

## 18. Command-line reference

The CLI is operational support around the MCP server. Except for `index build`
and config emission, commands are non-mutating.

### 18.1 Global options

| Option | Value | Meaning |
|---|---|---|
| `--version` | none | Print Lumen version and exit. |
| `--data-dir` | path | Override all normal state paths. |
| `--index` | path | Override the index file. |

### 18.2 `serve`

```text
lumen-mcp serve [--stdio | --http] [--host HOST] [--port PORT]
```

| Option | Default | Meaning |
|---|---:|---|
| `--stdio` | selected when neither flag is supplied | MCP on standard streams. |
| `--http` | false | Streamable HTTP at `/mcp`. |
| `--host` | `127.0.0.1` | HTTP bind host. Non-loopback unauthenticated values are refused. |
| `--port` | `8765` | HTTP port. |

### 18.3 `status`

```text
lumen-mcp status [--json]
```

Calls the same retrieval status implementation used by `lumen_status`.

### 18.4 `doctor`

```text
lumen-mcp doctor [--json]
```

Checks runtime directory, index existence, required SQLite catalog tables,
passage schema, MCP SDK generation/version, and retrieval status. It reports
`repairs_performed: []` and performs no repair.

### 18.5 `index build`

```text
lumen-mcp index build [--root PATH]... [--book-id ID]...
                      [--limit N] [--force] [--json]
```

| Option | Default | Meaning |
|---|---:|---|
| `--root` | all catalog roots | Repeatable exact catalog-root restriction. |
| `--book-id` | all books | Repeatable positive ID restriction. |
| `--limit` | `0` | Maximum examined books; zero means no limit. |
| `--force` | false | Rebuild even when fingerprint/extractor/chunker/coverage is current. |
| `--json` | false | Print final summary as JSON; otherwise progress goes to stderr. |

The builder streams one source at a time, checks size/mtime before and after,
stages a new revision, and activates only on success. The prior active revision
survives a failed replacement.

### 18.6 `config emit`

```text
lumen-mcp config emit --mode installed|development
  [--executable PATH] [--checkout PATH]
  (--output FILE | --stdout)
  [--force --backup] [--report text|json]
```

Emits strict UTF-8 JSON with one `mcpServers.lumen-book-reader` entry. Existing
different content is replaced only when both `--force` and `--backup` are
present. Replacement uses a same-directory temporary file, flush, fsync, and
atomic `os.replace`.

### 18.7 `config validate`

```text
lumen-mcp config validate --input FILE
  [--mode installed|development] [--json]
```

Rejects BOM/NUL/oversized bytes, duplicate JSON keys, extra fields, wrong
command/args/env, relative commands, absent executables, mode mismatch,
UNC/device commands, placeholders, and secret-like fields.

---

## 19. Operational research recipes

### 19.1 Find a known filename, then search inside it

1. `lumen_glob({"pattern":"**/*radio*","target":"path"})`
2. Take the returned `book_id`.
3. `lumen_search({"query":"receiver sensitivity","book_ids":[ID]})`
4. Read the best `resource_uri`.

### 19.2 Exact quotation verification

1. `lumen_grep` with `mode: "phrase"`.
2. Require a non-empty verified `match_ranges` array.
3. Cite `book` plus `locator` and retain `citation_id`.

### 19.3 Regex within one book

1. Discover the book ID with glob/search.
2. Call regex grep with that explicit ID.
3. Keep `fallback: "none"` when regex semantics are mandatory.

### 19.4 Compare an author's books

1. Find a representative book.
2. Call `lumen_related` with `relationship: "same_author"` and
   `exclude_same_book: true`.
3. Open only representative evidence from each returned book.

### 19.5 Expand a concept semantically

1. Call `lumen_search` with `strategy: "semantic"`.
2. Verify `backend.used` contains `wordnet-query-expansion`.
3. Use `lumen_grep` separately if exact terminology is also required.

### 19.6 Continue a large discovery query

1. Preserve the entire original request.
2. If `next_cursor` is non-null, repeat it with that cursor only.
3. Restart rather than editing a cursor after expiry/corpus/root changes.

---

## 20. Concurrency, memory, and index guarantees

| Concern | Guarantee |
|---|---|
| Reader concurrency | Bounded semaphore; default eight query-only SQLite connections. |
| SQLite writes from tools | None. |
| Sweep coexistence | WAL-aware readers; every query connection has its own snapshot/lifetime. |
| Query lock wait | Five seconds, with SQLite busy timeout. |
| Regex memory/work | Indexed candidates, 20,000-candidate and 4 MiB verification caps. |
| Search output | 100 hits maximum and 4,000-character excerpts. |
| Resource output | 65,536 bounded context. |
| Complete extraction | One document streamed into section-sized transactions. |
| EPUB safety | 512 MiB total expansion and 64 MiB per-section caps; safe normalized spine paths. |
| PDF safety | Page-by-page text extraction; password-protected sources never request credentials. |
| Revision safety | New revisions stage separately; activation is atomic. |
| Citation integrity | HMAC plus revision/content-hash verification. |
| Cursor integrity | HMAC plus query/corpus/root/expiry binding. |

---

## 21. Source implementation map

| Surface | Source file |
|---|---|
| Tlamatini descriptor | `LumenBookReader.json` |
| Tool registration/signatures | `lumen_reader/mcp_server/tools.py` |
| Resource registration | `lumen_reader/mcp_server/resources.py` |
| Prompt registration | `lumen_reader/mcp_server/prompts.py` |
| Server identity/policy | `lumen_reader/mcp_server/server.py` |
| SDK compatibility | `lumen_reader/mcp_server/compat.py` |
| CLI | `lumen_reader/mcp_server/cli.py` |
| Config generator/validator | `lumen_reader/mcp_server/config_export.py` |
| Diagnostics | `lumen_reader/mcp_server/diagnostics.py` |
| Retrieval facade/result shaping | `lumen_reader/retrieval/service.py` |
| Offline semantic expansion | `lumen_reader/retrieval/semantic.py` |
| Glob grammar | `lumen_reader/retrieval/glob_engine.py` |
| Exact/regex verification | `lumen_reader/retrieval/grep_engine.py` |
| FTS normalization | `lumen_reader/retrieval/lexical.py` |
| Query-only pool | `lumen_reader/retrieval/pool.py` |
| Citations | `lumen_reader/retrieval/citations.py` |
| Cursors | `lumen_reader/retrieval/cursors.py` |
| Passage schema/activation | `lumen_reader/passage_index.py` |
| Complete source builder | `lumen_reader/passage_builder.py` |
| Deterministic chunker | `lumen_reader/passage_chunker.py` |
| Runtime paths | `lumen_reader/runtime_paths.py` |
| Portable JSON Schema | `schemas/LumenBookReader.client.schema.json` |
| Frozen sidecar entry | `lumen_mcp.py` |
| Release packaging | `build.py` |

---

## 22. External-client conformance checklist

- [x] Descriptor contains exactly one `lumen-book-reader` server.
- [x] Command is an absolute dedicated Python executable.
- [x] Arguments are an array; no shell command string is used.
- [x] Environment contains only deterministic UTF-8/flush controls.
- [x] MCP SDK 2.1.1 is installed in the descriptor's environment.
- [x] The environment imports this exact checkout as Lumen 1.7.0.
- [x] STDIO initialization succeeds.
- [x] Exactly seven public tools are discoverable.
- [x] Every tool has read-only/non-destructive/idempotent/closed-world annotations.
- [x] Structured success and structured error delivery are implemented.
- [x] Root, book, cursor, citation, query, output, and concurrency bounds are enforced.
- [x] Exact literal/phrase ranges and bounded regex are implemented.
- [x] Offline WordNet semantic strategy is implemented.
- [x] Exact author identity and subject-label overlap are implemented.
- [x] Passage, section, book, citation, and status resources are registered.
- [x] The entire reader/MCP regression collection remains the release gate.

This document and `LumenBookReader.json` together are the complete handoff for
adding the Lumen Book Reader server as an external STDIO MCP in Tlamatini.
