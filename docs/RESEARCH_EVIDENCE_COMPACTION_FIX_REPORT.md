# Tabvis Long-Task Research Evidence, Incremental Writing, and Compaction Fix Report

> Date: 2026-07-26  
> Scope: `BrowserExtract`, browser artifacts, automatic compaction, session resume, and `Write` /
> `Edit` writing strategies  
> Conclusion: The issue has been fixed and both targeted and complete regression tests have passed.
> This report also explains why neither a 1M context window nor one enormous `Write` call should be
> treated as a persistence strategy.

## 1. Conclusion First

The user's diagnosis was correct, but the issue needs to be separated into two layers:

1. **The atomic `Write` capability did not corrupt files.** It creates or overwrites a file with
   complete content. Existing files must be read first, and modification-time and content-race checks
   run before and after writing.
2. **The Agent workflow of “browse for a long time, then write the entire report once at the end” was
   flawed.** When research exists only in the model context, output truncation, run cancellation,
   service restart, context compaction, or model hallucination can each cause the final report to
   fail or lose source attribution.

The correct fix is therefore not to raise `max_output_tokens` indefinitely, but to:

- Create an independent durable evidence checkpoint whenever the browser obtains a valid structured
  source.
- Create the report skeleton and source ledger early when the user explicitly requests a local
  report.
- Incrementally update the target report with a small `Edit` after every source that genuinely
  contributes evidence.
- Automatically restore URLs, dates, numbers, and body excerpts from durable checkpoints after
  compaction and resume.
- Use the final stage only for synthesis, deduplication, and consistency checks—not for the first
  write to disk.

## 2. Do Current Long Tasks Compact?

Yes. Automatic compaction is enabled by default unless one of the following is set:

- `DISABLE_COMPACT=1`: disable all compaction.
- `DISABLE_AUTO_COMPACT=1`: disable only automatic compaction.
- `autoCompactEnabled=false` in `settings.json`: disable automatic compaction.

Before **every model call**, the Agent estimates the token count of the current messages. The
calculation is:

```text
effective_window
  = context_window - min(model_output_default, 20,000)

auto_compact_threshold
  = effective_window - 13,000
```

For a common current model with a 1M context window and a default output of at least 20K, the
automatic compaction threshold is approximately:

```text
1,000,000 - 20,000 - 13,000 = 967,000 tokens
```

This does not mean the Agent should wait until 967K tokens before saving research. Compaction protects
context capacity; it is not a database transaction and should not be responsible for exact source
persistence. Tests can reduce the effective window with `TABVIS_AUTO_COMPACT_WINDOW`, or lower the
trigger percentage with `TABVIS_AUTOCOMPACT_PCT_OVERRIDE`.

The original compaction flow was:

1. Call a tool-free summarization model.
2. Replace old messages with the summary.
3. Restore a small set of recent files, plans, and skill attachments.
4. Include the original transcript path in the summary.

The risk is that a summary is lossy. It may preserve “researched a paper” while losing the paper URL,
version date, exact percentage, which source supports that number, and where writing stopped in the
target report.

## 3. The Original Failure Chain

The smart-contract research scenario exposed this sequence:

1. The Agent visited many pages and asked multiple research branches to collect material.
2. It did not attempt its first `Write` of the formal report until the end of the task.
3. A fixed local limit of 8,192 output tokens truncated the long tool JSON.
4. The primary transcript was not previously saved when the run was cancelled.
5. Continue after a service restart could not recover the complete research.
6. Without the original evidence, the model generated a draft containing `XXXXX` links and invented
   numbers.

Previously completed fixes:

- Changed the output budget from a fixed 8,192 to a value selected from model capabilities.
- Save messages that have entered the primary message chain in `finally` when a run is cancelled or
  closed.
- Ensure that “write the target report, but do not modify other files” is not misclassified by file
  policy as globally read-only.

This work further removes the design flaw in which all evidence depended solely on the transcript or
its compact summary.

## 4. Implementation

### 4.1 Automatically Persist Structured Evidence After Every BrowserExtract

After a successful `BrowserExtract`, the browser artifact now stores an additional `research` field
containing:

- URL and page title.
- Query, scope, and `scope_matched`.
- Readable body text.
- Matches, headings, and dates.
- Absolute links.
- Tables.

It uses the existing session-artifact location:

```text
<config-home>/projects/<project>/<session>/browser-artifacts/events.jsonl
```

This design has the following properties:

- Append-only: one source record does not disappear if later report writing fails.
- DLP-protected: data still passes through artifact DLP before being persisted.
- Bounded: one research checkpoint is limited to roughly 32K characters, with body text initially
  capped at 12K. If it remains too large, it shrinks deterministically in the order of tables, links,
  headings, then body text.
- Auditable: records remain associated with the action, session, agent, workspace, time, and artifact
  sequence number.
- Does not bypass user permissions to modify a workspace report.

Structured research evidence is saved only for successful `BrowserExtract` calls; every navigated
page is not automatically written into the formal report. Search-result pages, login pages,
CAPTCHAs, 404s, and irrelevant candidate pages are generally not evidence that should be cited.

### 4.2 Automatically Reinject Evidence Checkpoints After Compaction

After every successful automatic compaction, the run loop reads the current session's research
checkpoints:

1. Deduplicate by `URL + scope + query`, retaining the newest record.
2. Select at most the 12 most recent sources.
3. Build an evidence index of at most roughly 24K characters.
4. Inject it as a meta user message in the current conversation.
5. Explicitly label it `LOW-PRIVILEGE EXTERNAL DATA`; web content may serve as evidence but never as
   instructions.

Automatic reinjection includes:

- The durable JSONL path.
- Exact URL, title, and query.
- Dates, headings, and important links.
- Bounded body text and the first table.

When exact details are absent from the index, the Agent can read the JSONL or revisit the source
instead of guessing.

### 4.3 Restore the Same Evidence After Resume / Continue

In addition to loading the transcript, `stream_agent(resume=True)` now reads research artifacts from
the same session and places them, together with Agent Memory's `context_preamble`, at the beginning
of the **current user turn** as low-privilege context.

Sources can therefore be recovered after:

- The user cancels a stuck long-running run and clicks Continue.
- A service restart followed by reuse of the same session.
- An overly terse transcript summary.
- The browser page has closed while its artifact remains available.

### 4.4 Change Reports to “Skeleton First, Incremental Updates Later”

The system prompt and the tool descriptions for `BrowserExtract`, `Write`, and `Edit` now jointly
require:

1. Write a file only when the user explicitly requests that a report be saved or created.
2. Create the target file early, containing at least a table of contents and source ledger.
3. After each **valid source that genuinely supports a conclusion**, immediately record:
   - URL.
   - Title and publication date.
   - Verified facts and exact numbers.
   - Missing fields and uncertainties.
   - The conclusion it supports.
4. Use `Edit` for bounded updates.
5. Perform organization, deduplication, and executive-summary writing only at the end.

This is safer than overwriting the full report after every opened page. The latter repeatedly sends
an ever-growing file and may write irrelevant pages into the report.

### 4.5 Strengthen the Compaction Summary Prompt

Even when artifacts are disabled, the compaction prompt now requires preservation of:

- Exact source URL, title, and publication date.
- Numeric findings.
- The claim supported by each source.
- Target report path.
- Current source-ledger state.
- Most recently persisted evidence.

This is a second line of defense, but durable checkpoints remain the authoritative recovery source.

## 5. Why BrowserExtract Does Not Directly Modify the Target Report

Browser reading is a read-only `browser.read` operation; the user's target report is a
`filesystem.write` operation. Allowing `BrowserExtract` to write arbitrary user files internally
would create three problems:

1. It would bypass file-write policy, approval, Read-before-Edit, and concurrent-modification checks.
2. The tool cannot reliably know which target file the user authorized.
3. Prompt injection in a web page could use a browser-read operation to contaminate local files.

The implementation therefore separates responsibilities:

```text
BrowserExtract
    └─ Automatically writes an internal, DLP-protected, low-privilege evidence artifact

Agent + Edit/Write
    └─ Incrementally maintains the formal report in a user-authorized target file
```

The internal checkpoint supports recovery and auditing; it is not the final user deliverable.

## 6. Security Boundaries

- Web content is always labeled untrusted external data.
- Evidence context cannot override system or user instructions.
- Research artifacts continue to pass through DLP.
- Each checkpoint, the number of sources, and total reinjected characters are bounded.
- Automatic checkpoints do not modify the Git working tree.
- Formal reports must still pass through File Policy Guard.
- When a URL, number, or citation is missing, the Agent must read the artifact or revisit the source;
  guessing is prohibited.
- Only the bounded rendered-DOM structure returned by `BrowserExtract` is recorded; unrestricted full
  HTML is not injected into the model.

## 7. Test Coverage

New and updated targeted tests cover:

- BrowserExtract artifacts preserve dates, tables, links, and body text.
- Repeated extractions with the same URL/scope/query retain the newest version during reinjection.
- Both individual checkpoints and reinjected context respect character budgets.
- The evidence index contains the durable JSONL path and low-privilege label.
- The compaction prompt requires source provenance, numbers, and report progress.
- Post-compaction messages automatically contain durable research evidence.
- BrowserExtract tool results explicitly recommend incremental updates to local reports.
- Existing browser-artifact, DOM, DLP, Write, and cancellation-persistence tests do not regress.

Final regression results are listed in Section 10.

## 8. Recommended Execution Flow for Long Research Tasks

```text
Receive "research this topic and write a local report"
    ↓
Create report skeleton + Source Ledger
    ↓
Open source → BrowserExtract
    ├─ Application automatically writes a research checkpoint
    └─ Agent uses Edit to record citable facts from that source
    ↓
Continue to the next valid source
    ↓
Reach context threshold → compact
    ├─ Model summary preserves task/file progress
    └─ Application reinjects the durable evidence index
    ↓
Final synthesis, conflict checking, deduplication, and link/placeholder validation
```

The source ledger in the target report should contain at least:

| Field | Purpose |
|---|---|
| `source_id` | Stable reference, such as S1 or S2 |
| URL | Prevent confusion between similar titles and prevent model guessing |
| Title, publication date, version | Verify recency and whether a source is the “latest” |
| Access time | Allow dynamic pages to be rechecked |
| Exact facts / numbers | Prevent compaction from rewriting numbers |
| Supports | Record which conclusion the source supports |
| Limitations | Distinguish author-reported claims, vendor statements, and independent reproduction |

## 9. Future Productization Opportunities

This fix closes the evidence-loss path, but the experience can be improved further:

- Display `research checkpoints N` and the most recent sources on the Web detail page.
- Provide a first-class `ResearchLedger` tool so the Agent records claims and sources in structured
  fields instead of relying on a Markdown editing format.
- Write compaction events and restored checkpoint counts to the Gateway event log.
- Let users explicitly specify the target-report path in New run and display how many sources have
  been written incrementally.
- Add retention periods, export, and per-run filtering for artifacts.
- Show context window and maximum output tokens separately on the provider-capabilities page.

These are enhancements and do not affect the core guarantee after this fix: valid web evidence is
persisted before leaving the page and can be recovered after compaction, cancellation, and resume.

## 10. Final Verification

- Research checkpoint / BrowserExtract / compaction / cancellation-persistence targeted tests:
  24 passed.
- `uv run pytest -q`: 1,278 passed.
- `uv run ruff check .`: passed.
- `uv run python -m compileall -q tabvis`: passed.
- `web/npm run build`: TypeScript checks and the Vite production build passed.
- `git diff --check`: passed.
