# Agent Progressive Disclosure Audit and Fix Report

Date: 2026-07-26

## Conclusion

Before this fix, Tabvis already had well-layered designs for browser observation, skills, file
search, and context compaction. However, the most important capability—loading tool schemas on
demand—was not connected to primary model requests. Although `ToolSearch`, `should_defer`, and MCP
deferred markers existed, `model_client` still sent every tool schema to the model on every turn.
Progressive disclosure at the tool layer therefore did not actually work. In addition, the original
`tool_reference` result format only worked with endpoints that support that Anthropic beta content
block.

This work converts that path into genuine, cross-provider, server-side progressive loading and adds
bounded windows and continuation cursors for long-file reads. Overall support can now be rated
“good”: frequently used core capabilities are immediately available, infrequent or external
capabilities expand on demand, and discovered state survives automatic compaction and resume.

## Audit Matrix

| Layer | Before the fix | Result of this work |
|---|---|---|
| Tool schemas | Deferred markers existed, but primary requests still sent every schema | The first turn sends only core tools and `ToolSearch`; after a match, the next turn loads only the selected schemas |
| MCP tools | Marked deferred, but still included in the complete schema set | All MCP tools without `always_load` expose only their names by default and load by keyword or exact name |
| Provider compatibility | Depended on Anthropic `tool_reference` | Records selections in internal `toolUseResult` and returns plain text on the API wire; compatible with Anthropic, OpenAI-compatible providers, and Gemini |
| Compaction/resume | Compact could preserve legacy discovered names | The new format also reconstructs state from message history and continues using `preCompactDiscoveredTools` across compaction |
| File reading | Supported `offset`/`limit`, but attempted to read the entire file by default; paged results gave no continuation hint | Reads at most 2,000 lines by default and returns start/end lines, total lines, `nextOffset`, and the next-call hint; output is also bounded by byte/token windows |
| File search | `Grep` had `head_limit`/`offset`; `Glob` had a limit and truncation notice | Unchanged; this already provided reasonable progressive disclosure |
| Browser observation | Reference-tagged accessibility snapshots; sparse pages automatically supplemented with a screenshot and bounded HTML; research extraction supports query/scope | Keeps frequently used browser tools resident and avoids routinely injecting complete HTML into the model |
| Skills | Starts with a bounded list of names and expands to full instructions after invocation | Unchanged |
| Long context | Automatic compaction, microcompaction, and research-evidence checkpoint/restore | Unchanged; discovered tools do not depend on in-process memory |

## Root Cause

`tabvis/utils/tool_search.py` already contained mode detection and discovered-tool scanning, and
`tabvis/agent/tools/tool_search_tool.py` could search deferred tools. However,
`tabvis/agent/api/model_client.py` directly ran `tool_to_api_schema()` over the complete incoming
`tools` collection. The primary loop did not:

1. Determine whether tool search was enabled.
2. Filter deferred schemas on the first turn.
3. Restore the loaded set from historical `ToolSearch` results.

The existing mechanism therefore existed only in supporting analysis and compaction code; it did not
change the tools actually sent to the model.

## Implementation

### 1. Server-Side Schema Selection

The new `select_tools_for_model_request()` computes the model-visible set from complete message
history on every turn:

- Core tools and `ToolSearch` are always visible.
- `should_defer=True` tools and MCP tools without `always_load` are hidden on the first turn.
- Tools matched by `ToolSearch` become visible on the next turn.
- Tools that were previously called remain visible.
- `ENABLE_TOOL_SEARCH=false` restores complete loading.
- `auto` / `auto:N` continues using the existing threshold logic.

The tool executor still owns the complete registry. Filtering affects only the model API's schema
surface; it does not affect permission checks or the tool instances themselves.

### 2. Provider-Neutral Discovery State

`ToolSearch` returns:

- API wire: plain text describing which tools were loaded.
- Internal transcript envelope:
  `{"type":"tool_search_result","matches":[...]}`.

The next turn reads exact names from `toolUseResult`, so third-party gateways no longer need to
understand `tool_reference`. Legacy `tool_reference` entries from old sessions remain readable.

### 3. Names First, Schemas Later

The `ToolSearch` description contains a name list of at most 12,000 characters, without parameters
or full descriptions. If the list exceeds the limit, it explicitly shows the number omitted. Search
still runs against the complete server-side registry, so tools absent from the displayed list remain
discoverable by capability keywords.

### 4. Deferred Loading for Infrequent Tools

The following built-in tools are deferred by default:

- `Workflow`
- `NotebookEdit`
- `TodoWrite`
- `AskUserQuestion`

Frequently used tools for browser navigation, observation, and interaction, plus `Read`, `Grep`,
`Glob`, `Bash`, `Edit`, and `Write`, remain resident so ordinary browsing and coding tasks do not
require an extra search first.

### 5. Progressive Reading for Long Files

`Read` now honors the default behavior described by its tool hint:

- Reads only 2,000 lines when `limit` is omitted.
- Returns `startLine`, `endLine`, `totalLines`, `hasMore`, and `nextOffset`.
- The model-visible result explicitly recommends the next `Read offset=... limit=...` call.
- When output reaches the byte/token window, only complete lines are retained.
- If one line exceeds the window, the result avoids falsely claiming end-of-file and recommends
  `Grep` or a bounded byte inspection instead.

## Measurements

Using the same model and the same serialized tool descriptions with the current default tool pool
and no MCP connections:

| Metric | Complete set | Progressive first turn |
|---|---:|---:|
| Tool count | 22 | 18 |
| Schema characters | 59,015 | 37,247 |
| Reduction |  | **36.9%** |

The first-turn deferred tools are `Workflow`, `NotebookEdit`, `TodoWrite`, and `AskUserQuestion`.
After MCP connections are added, each MCP tool's full description and JSON Schema also stays out of
the first turn, so savings generally increase with the number of tools.

These character counts measure the actual JSON size produced by `tool_to_api_schema()`, not token
estimates returned by a model provider. The token savings vary slightly across tokenizers.

## Verification

New tests cover:

- First-turn schema deferral for third-party models and models without `tool_reference` support.
- The `always_load` MCP exception.
- A real `ToolSearch.call()` result driving the next turn's schema set.
- Loading only matched tools without leaking other deferred schemas.
- Preserving discovered tools across a compaction boundary.
- Complete fallback when `ENABLE_TOOL_SEARCH=false`.
- Ensuring the name list does not contain full descriptions.
- Ensuring the provider-wire result contains no `tool_reference`.
- Deferring `Workflow` by default.
- `Read` defaults for 2,000 lines, continuation cursors, final pages, and oversized single lines.

## Remaining Boundaries

- A browser snapshot is a bounded observation of the current page, not an automatic crawl of the
  entire site. Long pages should use `BrowserExtract(query=..., scope=...)`, scrolling, or navigation
  to narrow the scope further.
- The `ToolSearch` name list has a 12,000-character limit. Names omitted from extremely large MCP
  pools remain searchable by capability keywords, but the model must first know which capability it
  needs.
- Compact summaries remain lossy. Exact research evidence belongs in durable browser-research JSONL
  and report files rather than retaining all historical HTML indefinitely.
