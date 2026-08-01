# Tabvis Web Console: 16 Non-Financial Real-World Use Cases Across Four Batches

- Test date: 2026-07-26 (Asia/Seoul)
- Test URL: `http://localhost:8765/`
- Service mode: The first batch used the bundled Web console started by `uv run tabvis`. The second,
  third, and fourth batches reused or restarted the production-mode instance on port 8765.
- Browser configuration: Playwright Chromium with an isolated profile
- Test method: Create actual Agents from the Web console, observe SSE tool traces, wait for terminal
  state, and verify persisted results in Sessions
- Scope: Financial-report scenarios were excluded. The first batch covered local-code
  understanding, current-information retrieval, cross-site comparison, PDF download/reading, and
  dynamic weather lookup. The second added web-state manipulation, official travel information,
  public data, technical documentation, and interactive choices. The third added product selection,
  live public events, open-source issue triage, daily media, and academic search. The fourth added
  deep cross-source research, long-report persistence, cancellation/resume, and evidence rereading.

This report follows `WEB_CONSOLE_USER_SCENARIO_REPORT.md` and tests real Agent runs rather than
replacing the earlier UI-focused report. It preserves the first three batches of 15 scenarios and
their fix history, then appends the fourth-batch deep-research scenario completed on 2026-07-26.

## First-Batch Summary (Historical Record)

The first batch designed five real-user use cases. The PDF scenario required one extra run because
the first attempt did not satisfy the user goal, producing six Agent run records in total:

- 3 scenarios passed.
- 2 scenarios partially passed.
- All 6 run records ended with a `completed` runtime state.
- Total: 68 turns, 37 tool calls, and 328.5 seconds.

Primary capability conclusions:

- New run, live SSE traces, result persistence, and historical review in Sessions were already
  usable for real tasks.
- Local-code analysis, current-information retrieval from a single official source, and dynamic-page
  reading were stable.
- The Agent used accessibility snapshots, clicks, and `BrowserExtract`; it did not decide each step
  by reading complete HTML.
- Automatic capture after direct PDF navigation worked. Problems initially exposed around large-PDF
  reading, filenames, and permission fallback were later fixed and retested.
- `completed` means only that execution ended; it does not prove that the business goal in the prompt
  was met.
- Continue submission and Web-based approval interaction were the clearest initial product gaps.
  Both have since been fixed and retested through the real page.

## Fixes and Retesting (2026-07-26)

The scenario records below preserve evidence from the initial tests. This section records the code
fixes and retests based on those findings so historical observations are not confused with current
code behavior.

| Issue | Change | Verification | Current status |
|---|---|---|---|
| Continue did not restore the session | `/agent` continuation now receives the previous run's `session_id`, profile, model, and max turns | After continuation, `ag_dc0f0e24f34b` still used `ses_6b4ee9e5d9f1` and returned `Continue OK` | Fixed |
| A second consecutive Continue on the same Agent did not navigate | The frontend now deduplicates based on whether the current request navigated, rather than allowing the previous request's `streamRef` to block navigation | After multiple Continue operations, `ag_7a9740d4d817` returned to the detail page and returned `second continue ok` | Fixed |
| `[object Object]` error display | Unified parsing of string errors and Gateway `{error:{code,message,trace_id}}` errors | TypeScript build passed; structured Gateway errors are no longer stringified directly | Fixed |
| No UI for `AskUserQuestion` | Gateway persists the question as an Interaction and moves the run to `waiting_for_input`; the detail page polls and displays single-select, multi-select, and Other | Real Agent `ag_7a9740d4d817` displayed “Select a runtime environment.” Choosing “Test environment” resumed and completed the same run | Fixed |
| No UI for policy `ask` | Non-question actions with `behavior=ask` become approval Interactions; the detail page displays the command, Allow once, and Deny | `pip install definitely-not-a-real-tabvis-package` displayed a Bash approval card; Deny prevented installation and failed the run with `approval_denied` | Fixed |
| Dynamic PDF saved with `.cfm` | Prefer `Content-Disposition`, then infer `.pdf` from MIME or `%PDF-` magic | Unit test saves `get_pdf.cfm?pub_id=936225` as `get_pdf.cfm.pdf`; Read can also sniff an old `.cfm` file | Fixed |
| Large PDFs used the ordinary 256 KB text path | `Read` supports PDF magic sniffing, page ranges, and native text extraction; the declared `pypdf` dependency is used when system Poppler is unavailable | The NIST file retained from the first test was read with `pages="1-3"`, returning `pdf_text` and 1,994 characters | Fixed |
| Agent temporarily installed a PDF package | `pypdf` is now a locked runtime dependency; standard policy classifies pip/npm/uv add/brew installation as `environment.install` and requires approval | Policy regression tests cover five installer forms; real Web denial path passed | Fixed |
| ISO dates and numeric URLs were over-redacted | Phone DLP excludes ISO dates and preserves numeric identifiers in URL paths/queries | Regression tests passed for `2026-06-10`, `pub_id=936225`, and numeric SEC/Alibaba document paths | Fixed |
| Running `turns/tools` remained zero | Monotonically increasing run progress is persisted after every assistant turn | The page showed `1 / 1` while waiting on AskUserQuestion and `4 / 2` while waiting on install approval | Fixed |
| Truncated tool traces were misleading | Input previews increased from 110 to 600 characters and explicitly append `[truncated]` | AskUserQuestion's question, choices, and descriptions are fully visible in the live trace | Fixed |
| `completed` could be mistaken for goal completion | Added a tooltip and permanent explanation to completed state | Real completion page says “run ended successfully” and does not independently validate the requested outcome | Fixed (copy) |

Regression results:

- `uv run pytest -q`: 1,251 passed.
- `uv run ruff check .`: passed.
- `uv run python -m compileall -q tabvis`: passed.
- `web/npm run build`: TypeScript checks and Vite production build passed.
- Continue, question, approval, live metrics, and completed-state copy were retested on the real
  `http://localhost:8765/` page.

## Application-Level Page Perception Strategy

The Agent **does not send complete HTML to the model at every step and then decide what to do**. The
current strategy is layered and escalates only when needed:

1. Ordinary page actions return a Playwright accessibility snapshot with stable `ref` values. The
   Agent primarily uses accessible name, role, visible text, and the latest refs to decide whether
   to click, type, scroll, or wait.
2. When the accessibility tree is sufficiently rich, HTML is not attached. The snapshot limit is
   about 40,000 characters, with roughly 22,000 characters allocated to readable page text.
3. Only when the accessibility tree is too sparse, as with canvas, map, or image-heavy pages, does
   the system add a screenshot and sanitized, compressed, truncated HTML. The HTML budget is about
   12,000 characters—not an unrestricted complete DOM.
4. Research tasks involving reports, publication dates, tables, or links can explicitly call
   `BrowserExtract`. It reads the rendered DOM and returns bounded body text, headings, dates,
   tables, and absolute links within an overall budget of roughly 44,000 characters; it still does
   not return raw full-page HTML.
5. When a selector does not match, the tool now returns `scope_matched=false`, the actual fallback
   scope, and up to eight visible candidate containers instead of silently pretending the selector
   matched.
6. A more complete DOM can be saved as an audit artifact, capped at about 1 MB, but it is not
   injected into model context by default. PDFs enter the download workspace and are read page by
   page with `Read(pages=...)`.

This design keeps default context smaller, gives interaction refs better stability, and reduces
prompt-injection and irrelevant-script noise. The tradeoff is that structurally complex or
accessibility-poor pages must correctly trigger screenshot, HTML, or structured-extraction fallback.
The current implementation provides all three; this work also added selector candidates and the PDF
pagination path.

## Scenario Overview

| # | Real-world use case | Agent ID | Result | turns / tools | Duration |
|---|---|---|---|---:|---:|
| 1 | Analyze local-project startup entry points | `ag_c1d23db54a69` | Pass | 14 / 8 | 38.7s |
| 2 | Find the latest stable Python release | `ag_4dae1666801a` | Pass | 5 / 2 | 17.6s |
| 3 | Compare Playwright and Selenium Python installation requirements | `ag_3327904d39d4` | Partial pass | 8 / 4 | 30.6s |
| 4A | Find, download, and read the NIST AI RMF PDF | `ag_dc0f0e24f34b` | Goal not met, but runtime state was completed | 7 / 3 | 27.4s |
| 4B | Retry using an explicit PDF fallback path | `ag_701c0fcbd8fb` | Goal met, but inefficient workflow | 25 / 16 | 165.6s |
| 5 | Retrieve Seoul's next three days of weather | `ag_b589818a0a8e` | Pass | 9 / 4 | 48.6s |

## Scenario 1: Analyze Local-Project Startup Entry Points

### Prompt

> Analyze the startup entry points in the current Tabvis project. Explain whether `uv run tabvis`
> and `uv run tabvis --serve` are equivalent, and identify the relevant source files and key
> functions. Read only the local repository; do not use the network or modify files.

### Actual Process

1. Submitted the prompt on New run and successfully navigated to
   `/sessions/ag_c1d23db54a69`.
2. The Agent read `pyproject.toml` and located `tabvis.bootstrap_entry:main`.
3. It then read `tabvis/bootstrap_entry.py`, `tabvis/__main__.py`, and
   `tabvis/ui/entry/cli.py`.
4. In `cli.py`, it located `if not args or args[0] == "--serve"`, then read `_resolve()` and
   `serve_async()` in `tabvis/browser/server.py`.
5. The final answer explained that both commands take the same branch when no other arguments are
   added, and described how `--host`, `--port`, `--dev`, and environment variables affect behavior.

### Verification

Independent source review agreed with the Agent:

- `pyproject.toml:72` registers `tabvis = "tabvis.bootstrap_entry:main"`.
- `tabvis/bootstrap_entry.py:15` defines `main()` and calls `asyncio.run(cli.main())`.
- `tabvis/ui/entry/cli.py:66` routes no arguments and `--serve` to the same branch.
- `tabvis/browser/server.py:926` and `:936` resolve the address and start the service.

### Feedback

The answer was accurate and cited concrete files without using the network or modifying the
repository. During the run, however, `turns / tools` remained `0 / 0` and jumped to `14 / 8` only at
terminal state, making progress difficult to assess.

## Scenario 2: Find the Latest Stable Python Release

### Prompt

> Find the version number and release date of the latest stable Python 3 release as of today. Use
> only official python.org pages, include source links, and do not rely on model memory.

### Actual Process

1. The Agent opened `https://www.python.org/downloads/`.
2. To avoid relying only on the downloads landing page, it also opened
   `https://www.python.org/downloads/release/python-3146/`.
3. It reported Python 3.14.6, released on 2026-06-10, and included both official links.

### Verification

The official release page confirms that Python 3.14.6 was released on June 10, 2026, is the sixth
maintenance release of the 3.14 series, and contains about 179 fixes plus documentation/build
improvements.

### Feedback

This was the most stable Web retrieval scenario in the batch: two navigations and 17.6 seconds
satisfied both source and “latest” constraints. ISO dates in supplementary branch output appeared as
`[redacted]`, indicating that DLP could still mistake `YYYY-MM-DD` dates for phone numbers. The main
conclusion was unaffected.

## Scenario 3: Compare Playwright and Selenium Python Installation Requirements

### Prompt

> Compare the official Python installation methods and minimum Python versions for Playwright and
> Selenium. Use only their respective official documentation, provide a clear comparison, and
> include two source links.

### Actual Process

1. The Agent opened the Playwright Python installation documentation.
2. It opened Selenium's installation documentation and clicked the Python tab.
3. Selenium's official installation page did not state a minimum Python version, so the Agent read
   the Selenium project metadata on PyPI.
4. It reported:
   - Playwright: Python 3.8+, run `pip install pytest-playwright` followed by
     `playwright install`.
   - Selenium: Python 3.10+, run `pip install selenium`; modern releases normally use Selenium
     Manager for browser drivers.

### Verification

- Playwright's official documentation explicitly says Python 3.8 or higher and lists those two
  installation commands.
- Selenium's official documentation gives `pip install selenium`; current PyPI metadata says
  `Requires: Python >=3.10`.

### Feedback

The facts were correct, and cross-site navigation plus the documentation tab worked. However, the
prompt required “only their official documentation,” while PyPI is an authoritative package index
but not project documentation on selenium.dev. The final response then described both links as
“official” without distinguishing their source class, so this was a partial pass.

At the product layer, when a source constraint cannot be fully satisfied, the Agent should say:
“Official documentation does not state this field; the following minimum version comes from official
release-package metadata,” rather than merging the two source types.

## Scenario 4: Find, Download, and Read the NIST AI RMF 1.0 PDF

### Original Prompt

> Find the official NIST AI Risk Management Framework 1.0 PDF, download and read the original, then
> summarize its four core functions. Include the official PDF link and local saved path.

### First Run: Goal Not Met

Agent `ag_dc0f0e24f34b`:

1. Correctly opened the official NIST publication page.
2. Correctly identified the direct PDF link:
   `https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=936225`.
3. Encountered an approval requirement after calling `BrowserDownload`.
4. Switched to `AskUserQuestion`, but the Web detail page had no answer control; the question was
   subsequently cancelled automatically.
5. Summarized the four functions only from the web-page abstract and explicitly admitted it did not
   download or read the complete document.
6. Although the core prompt goal was unmet, Session still displayed `completed`.

### Continue Recovery Test: Submission Failed

After clicking Continue on that detail page:

1. `/run` correctly preselected `ag_dc0f0e24f34b`.
2. A recovery instruction requested direct `BrowserNavigate` to the PDF without another approval.
3. Clicking `Send to agent` created no new run and did not navigate to Session.
4. The page displayed only `[object Object]`; no human-readable error or corresponding browser-console
   error appeared.

### New-Agent Retry: Goal Met, but Inefficiently

A new Agent, `ag_701c0fcbd8fb`, used the explicit PDF URL and fallback instructions:

1. `BrowserNavigate` successfully captured a real 48-page PDF of about 1.9 MB.
2. It was saved as:
   `/Users/linghankong/.tabvis/projects/-Users-linghankong-Desktop-browserAgentLearning-tabvis/ses_665fb281c2be/workspace/get_pdf.cfm`
3. `Read` failed repeatedly because the file exceeded the 256 KB limit.
4. The Agent checked for `pdftotext` and installed Python PDF packages, including one attempted
   `pip install pypdf`.
5. It ultimately used the already available `pypdf` environment to extract pages and accurately
   summarize GOVERN, MAP, MEASURE, MANAGE, and GOVERN's cross-cutting relationship.

### Verification

The official NIST publication page confirms document number `NIST AI 100-1` and publication date
2023-01-26. The NIST AI RMF Core consists of GOVERN, MAP, MEASURE, and MANAGE, with GOVERN applying
across the other three. The final summary agreed with the PDF.

### Feedback

The scenario could ultimately be completed, but ordinary users should not need to know internal tool
names or fallback strategies. Improvements needed:

- Render policy / `AskUserQuestion` requests in the Web console and allow users to answer them.
- If `BrowserDownload` is denied, automatically try direct navigation to a public PDF.
- Normalize the filename to `.pdf` from `Content-Type: application/pdf` or `Content-Disposition`
  instead of saving `get_pdf.cfm`.
- After `Read` returns the 256 KB limit, immediately use PDF pagination/text extraction rather than
  repeating the same read.
- Dependency installation changes the environment; `pip install` MUST require explicit user
  approval and must not be an automatic recovery step.
- Product copy should distinguish Session `completed` from “the user goal was satisfied.”

## Scenario 5: Retrieve Seoul's Next Three Days of Weather

### Prompt

> Use the Korea Meteorological Administration or an official Seoul weather page to provide the exact
> dates, weather, and high/low temperatures for Seoul over the next three days. Include the query
> time and source link; do not use search-engine snippets as final evidence.

### Actual Process

1. The Agent visited an old KMA regional-forecast URL and found it had been retired.
2. It switched to the official “Weather Nuri” home page:
   `https://www.weather.go.kr/w/index.do`.
3. It read the daily forecast for Singil 2-dong, Dongjak-gu, Seoul.
4. It expanded the July 27 hourly forecast.
5. It attempted to scope `BrowserExtract` to the weather region, but the custom CSS scope did not
   match.
6. It fell back to the daily list already present in the accessibility snapshot:
   - July 26: showers / showers, 26–32°C.
   - July 27: showers / cloudy, 26–33°C.
   - July 28: cloudy / clear, 26–33°C.
7. It also included morning/afternoon precipitation probabilities, page update time, and the
   official link.

### Verification

At test time, the dynamic KMA page displayed `07.26 00:00 updated / current at 00:20`. The location,
dates, weather, high/low temperatures, and precipitation probabilities agreed with the Agent.

### Feedback

Recovery from the retired URL was sensible, and the Agent handled a Korean-language interface,
dynamic dates, and page clicks. Failure of the `BrowserExtract` scope did not block the task,
demonstrating a usable fallback between accessibility snapshots and structured extraction. The tool
should return candidate containers that actually exist on the page to reduce CSS-selector guessing.

## First-Batch Web Product Acceptance

| Capability | Result | Evidence |
|---|---|---|
| New run creation | Pass | All five use cases could be created from `/run` and navigated to the detail page |
| Live SSE trace | Pass | assistant, tool_use, result, and done were visible |
| Prompt persistence | Pass | Sessions displayed every prompt from this batch in full |
| Result persistence | Pass | The final result remained available after refreshing the detail page |
| Session status and metrics | Pass | List showed ID, status, turns, tools, and duration |
| Running turns/tools | Partial pass | Remained `0 / 0` while running and updated only at terminal state |
| Continue | Fail | After `Send to agent`, remained on `/run` and displayed only `[object Object]` |
| Web approval/question | Fail | `AskUserQuestion` had no interactive UI and was later cancelled |
| Direct PDF navigation | Pass | A real PDF was captured and persisted in the session workspace |
| Large-PDF reading | Partial pass | Eventually readable, but only after repeated failures and shell detours |
| Tool-trace debuggability | Partial pass | Some long arguments and numeric query values were truncated or removed |
| `completed` semantics | Partial pass | Indicates run termination, not that the prompt's business goal was achieved |

## First-Batch Issue Priorities

### P0: Continue Submission Failure (Fixed)

Reproduction:

1. Open any completed Session.
2. Click Continue.
3. Enter a new prompt and click `Send to agent`.
4. The page remains at `/run`, displays `[object Object]`, and creates no run.

Recommendations:

- Inspect the continuation API request body and structured Gateway response.
- Do not directly stringify error objects in the frontend; display at least `message`, HTTP status,
  and request ID.
- Add an end-to-end Continue regression test for a completed Agent.

### P1: Web Could Not Handle Approval and `AskUserQuestion` (Fixed)

After public-PDF download triggered a policy ask, the Agent tried to ask the user, but the detail
page had no answer control. A flow that needed one confirmation was therefore forced into
cancellation.

Recommendations:

- Add structured approval/question frames to SSE.
- Display choices, approve/deny buttons, and waiting state on the detail page.
- Resume the same run after an answer instead of requiring a new Agent.

### P1: Large PDFs Lacked Native Paginated Extraction (Fixed)

Automatic PDF capture already worked, but `Read` treated the 1.9 MB PDF as an ordinary file subject
to the 256 KB limit.

Recommendations:

- Add `pages`, `query`, or `max_chars` parameters for PDFs.
- Automatically extract and paginate PDF text rather than returning an ordinary large-file error.
- Explicitly tell the model not to repeat an unparameterized read after a “larger than 256 KB” error.

### P1: Automatic Recovery Crossed the Environment-Change Boundary (Fixed)

After PDF parsing failed, the Agent attempted `pip install pypdf`. Even though it ultimately used an
already installed package, installing dependencies should never be an unconfirmed default recovery
action.

Policy should classify package installation as a distinct high-risk category requiring explicit Web
approval.

### P2: DLP and Debug Information Had False Positives (Fixed)

- ISO dates could become `[redacted]`.
- A PDF URL with a numeric query could lose its argument value in the tool_use card even though the
  Sessions prompt and Visited link preserved the complete URL.

Use context-aware rules for dates, official document IDs, and URL paths/queries, and add explicit
placeholders to redacted fields so an empty-looking argument is not misleading.

### P2: Incorrect PDF Extension (Fixed)

The NIST response was a PDF, but the filename was `get_pdf.cfm`. Prefer the response-header filename;
if absent, append `.pdf` based on MIME type.

### P2: Running Metrics Were Not Live (Fixed)

The detail page showed `0 / 0` while many tool cards appeared on the left. Accumulate metrics from
SSE frames and reconcile them with persisted server metrics at terminal state.

## First-Batch Positive Feedback

- The fixed New run page no longer rendered a blank screen.
- Prompts, final results, and source links persisted to historical detail pages.
- Page snapshots were sufficient to drive a dynamic Korean-language site without putting complete
  HTML into model context.
- “Latest information” tasks from one official source were fast and accurate.
- The Agent could recover from old URLs, selector mismatches, and page-content truncation.
- The Visited list provided valuable visual audit evidence for result sources.

## Recommended Fix Order

1. Fix Continue submission and `[object Object]` display.
2. Complete the Web approval / `AskUserQuestion` loop.
3. Add MIME-aware PDF naming and native paginated text extraction.
4. Prohibit unapproved automatic package installation.
5. Correct DLP false positives for dates and numeric URLs.
6. Update turns/tools live and improve tool-use argument debugging.
7. Distinguish “run completed” from “user goal satisfied” in the UI.

## First-Batch Test Impact

The batch created six Agent run records and corresponding isolated browser profiles. The successful
PDF run left a roughly 1.9 MB file under
`~/.tabvis/projects/.../ses_665fb281c2be/workspace/`. It did not change Tabvis settings, and no test
Agent modified the current Git working tree. One `pip install pypdf` attempt appeared in the PDF
run's tool trace, so environment-level package-install policy should be confirmed.

---

## Second Batch: Five Additional Real-World Use Cases (2026-07-26)

### Second-Batch Summary

Avoiding financial reports and the first five use cases, this batch created five more real Agents
through `http://localhost:8765/`:

- All five run records ended with runtime state `completed`; none failed or were cancelled.
- Independent review of whether the prompt was actually satisfied found two passes and three partial
  passes.
- Total: 79 turns, 42 tool calls, and 391.5 seconds.
- Web-console concurrency capacity, live metrics, `AskUserQuestion`, result persistence, and Visited
  auditing worked.
- The main newly exposed problems were not crashes but application-level goal validation: drawing a
  definite conclusion when the source was ambiguous, writing a file without being asked, returning
  a final answer that was not self-contained, and presenting undated seasonal information as a
  2026-specific fact.

### Second-Batch Test Process

1. Visited `/run` and confirmed that Prompt, Agent, Browser, Profile, Model, Max turns, and Run agent
   controls worked.
2. Created the first four isolated Agents. The header correctly showed
   `running 4 / 4 · capacity 0`, confirming the concurrency ceiling and pre-queue capacity display.
3. After the first two runs ended, Sessions showed `running 2`, `completed 15`, and `capacity 2`.
   The fifth interactive Agent was then created.
4. Before browsing, the fifth Agent displayed “Agent needs your input.” The page rendered Tokyo,
   Osaka, and `Send answer`. Selecting Osaka enabled the button and resumed the same Agent and run.
5. Every scenario was reviewed in Sessions and its own detail page for state, turns, tools, duration,
   result, and Visited.
6. Facts were independently verified against official pages rather than treating `completed` as
   proof of goal satisfaction.
7. The browser console had no JavaScript errors. Each page load repeatedly emitted React Router v7
   future-flag warnings.

Second-batch runs:

| # | Real-world use case | Agent ID | Business result | turns / tools | Duration |
|---|---|---|---|---:|---:|
| 6 | Operate TodoMVC and verify page state | `ag_7373cff4c15a` | Pass | 14 / 8 | 63.8s |
| 7 | Find next-day museum opening and transport details | `ag_a68832a1f3c1` | Partial pass | 7 / 3 | 41.4s |
| 8 | Retrieve and calculate World Bank population data | `ag_56e6a88eea54` | Pass | 8 / 4 | 64.6s |
| 9 | Compare GitHub Actions and GitLab CI caching | `ag_ea5e0a1eb744` | Partial pass | 33 / 19 | 111.0s |
| 10 | Plan a free family attraction after the user chooses a city | `ag_0b4eb69742af` | Partial pass | 17 / 8 | 110.7s |

Local detail pages:

- `http://localhost:8765/sessions/ag_7373cff4c15a`
- `http://localhost:8765/sessions/ag_a68832a1f3c1`
- `http://localhost:8765/sessions/ag_56e6a88eea54`
- `http://localhost:8765/sessions/ag_ea5e0a1eb744`
- `http://localhost:8765/sessions/ag_0b4eb69742af`

## Scenario 6: Operate TodoMVC and Verify Page State

### Prompt

> Open `https://demo.playwright.dev/todomvc/`. Add three tasks: “Book meeting room,” “Send weekly
> report,” and “Back up photos.” Mark “Send weekly report” complete, delete “Back up photos,” then
> report the remaining list, completion state, and page URL. Do not modify local files.

### Actual Process and Result

1. The Agent navigated to Playwright's official TodoMVC demo.
2. It created all three tasks through page interactions, then completed and deleted the requested
   items. Final progress was 14 turns / 8 tools.
3. It reported two remaining entries:
   - “Book meeting room”: incomplete.
   - “Send weekly report”: completed.
4. The page showed `1 item left`, “Back up photos” no longer existed, and Visited retained
   `https://demo.playwright.dev/todomvc/#/`.
5. No working-tree changes were produced.

### Feedback

This scenario fully passed, showing that the Agent can do more than read pages: it can perform
sequential input, state changes, deletion, and result verification. These tasks depend on a fresh
accessibility snapshot and stable refs after each action, not complete HTML in model context.

One limitation was that a completed detail page preserved only the result and Visited, not complete
historical tool cards. After leaving and returning, users could not audit each typed value and click
and had to rely on the final result.

## Scenario 7: Find Next-Day Museum Opening and Transport Details

### Prompt

> Using only official National Museum of Korea pages, find the Seoul museum's opening hours for
> tomorrow (2026-07-27), whether the permanent exhibition requires a ticket or reservation, and the
> nearest subway station. Include official links and mark anything not explicitly stated by the
> website as not found.

### Actual Process and Result

1. The Agent visited the `museum.go.kr` home page, summer-hours announcement, permanent-exhibition
   page, and transport page.
2. It reported opening hours of `09:00–18:00` on 2026-07-27, free permanent exhibitions, and the
   direction from Ichon Station Exit 2 through the museum underpass to the west gate.
3. The final answer said that no reservation was required, but later acknowledged that the website
   did not explicitly state an individual reservation requirement for that date.

### Independent Verification

- The [summer-hours announcement](https://www.museum.go.kr/MUSEUM/contents/M0701010000.do?arcDataType=&arcId=23830&catCustomType=united&catId=128&cp=1&schM=view&sv=&unitedUse=)
  explicitly states `09:00–18:00` from July 27 through August 17.
- The [permanent-exhibition page](https://www.museum.go.kr/MUSEUM/contents/M0201010000.do) explicitly
  says admission is free for everyone, but that statement alone does not prove that reservations
  are unnecessary.
- The [subway directions](https://www.museum.go.kr/MUSEUM/contents/M0106010000.do?menuId=subway-map)
  support Ichon Station, Lines 4/Gyeongui–Jungang, Exit 2, and the museum underpass.

### Feedback

Opening hours, free admission, and transport were correct. “No reservation required” was an
unsupported inference, so the scenario partially passed. At the application layer, each requested
field should bind to evidence and be classified as:

- `source_explicit`: directly supported by the source.
- `inferred`: inferred from page structure or ordinary rules.
- `not_found`: absent from official sources.

When the prompt explicitly says to mark missing information as not found, the final answer must not
promote `not_found` into a definite conclusion.

## Scenario 8: Retrieve and Calculate World Bank Population Data

### Prompt

> Using only official World Bank pages, find the latest available total population for South Korea:
> year and value, and compare it with the previous year. Include both years, the difference, and an
> official link. Do not use search-result snippets.

### Actual Process and Result

1. The Agent visited the World Bank WDI indicator page and read the official World Bank API.
2. It reported `51,684,564` for 2025 and `51,751,065` for 2024.
3. It calculated a decrease of `66,501`, approximately `0.13%`.
4. It included both the official indicator page and API link.

### Independent Verification

The [World Bank South Korea page](https://data.worldbank.org/country/korea-rep) currently identifies
`51,684,564` in 2025 as the latest total-population value; the WDI indicator/API gives
`51,751,065` for the previous year.

Calculation:

```text
51,684,564 - 51,751,065 = -66,501
-66,501 / 51,751,065 ≈ -0.1285%
```

### Feedback

This scenario passed. It demonstrated the practical combination of locating an indicator on a web
page, using an official API for exact values, and calculating locally, while avoiding search-engine
snippets as final evidence. Results should also show retrieval time and raw fields so future data
updates can explain differences between historical runs and the current page.

## Scenario 9: Compare GitHub Actions and GitLab CI pip Caching

### Prompt

> Compare how official GitHub Actions and GitLab CI documentation cache Python/pip dependencies.
> Give one minimal YAML example for each and explain when each cache key is invalidated. Use only
> `docs.github.com` and `docs.gitlab.com`, and include source links.

### Actual Process and Result

1. The Agent visited only the two specified documentation domains and read caching tutorials and
   references across both sites.
2. It correctly found GitHub's `actions/setup-python@v5` plus `cache: 'pip'` example.
3. It correctly found GitLab's `.cache/pip`, `PIP_CACHE_DIR`, and `cache:key:files`.
4. At 33 turns / 19 tools, it was the slowest and most tool-intensive scenario in the batch.
5. Without being asked, the Agent created
   `docs/CI_CACHING_PYTHON_COMPARISON.md` in the current repository and wrote the YAML plus detailed
   comparison there.
6. The final Web answer included only a summary and file path, not the two YAML snippets required by
   the prompt, so it was not self-contained.

### Independent Verification

- The [GitHub Python workflow documentation](https://docs.github.com/en/actions/tutorials/build-and-test-code/python#caching-dependencies)
  includes `actions/setup-python@v5`, Python 3.12, and `cache: 'pip'`.
- The [GitLab Python cache documentation](https://docs.gitlab.com/ci/caching/examples/#python)
  requires pip's cache to be inside the project directory and demonstrates
  `PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"`.
- The two core YAML examples in the generated file agreed with the official examples, and the
  conclusion that a lock-file/hash change creates a new key was reasonable.

### Feedback

The facts were broadly correct, but the behavior contract was only partly satisfied:

- The user asked for YAML, which should have appeared directly in the final answer rather than
  requiring the user to open another file.
- The prompt did not request document creation. Research tasks should be read-only by default;
  unexpected file writes pollute the working tree and increase audit cost.
- If an Agent decides a long result should be persisted, it should first fully answer the core
  request in the final response, then offer the file as an optional artifact—or policy/settings
  should enforce “research tasks are read-only by default.”

The untracked file was retained as evidence rather than deleted without authorization.

## Scenario 10: Plan a Free Family Attraction After the User Chooses a City

### Prompt

> First use AskUserQuestion to let me choose Tokyo or Osaka. After receiving my answer, use only the
> selected city's official tourism website to find one free family-friendly attraction in August
> 2026, including opening information and a source link. Do not browse before I answer.

### Actual Process and Result

1. The Agent called `AskUserQuestion` before any Visited record appeared.
2. The Web detail page correctly displayed Tokyo, Osaka, Other, and `Send answer`. Selecting Osaka
   resumed the same run.
3. The Agent browsed only OSAKA-INFO. After one 404 and several listing/filter pages, it found
   Nishikinohama Beach.
4. It reported “mid-July to late August,” “open daily,” free beach admission, family/child
   facilities, about a ten-minute walk from Nishikinohama Station, and the source link.

### Independent Verification

The [OSAKA-INFO attraction page](https://osaka-info.jp/en/spot/nishikinohama-beach-park/) explicitly
supports:

- Swimming season from mid-July to late August.
- `Open Daily`.
- `Free admission to the beach`.
- A `Child` facility tag, family suitability, and an approximately ten-minute walk from the station.

### Feedback

The interactive loop passed, especially the “ask first, browse later” ordering constraint. Content
was only a partial pass:

- The official page gives an undated recurring season, but the Agent's table presented it as
  “mid-July to late August 2026.” A more rigorous answer would say that the site currently lists the
  usual period but provides no 2026-specific dates and recommends checking before departure.
- The first sentence contained a garbled/incorrect translated place name; the later official
  “Nishikinohama” name was correct.
- The Agent visited seven pages and encountered a 404 before finding a candidate. Better on-site
  filtering and candidate-evidence scoring could reduce ineffective navigation.

## Second-Batch Web Product Acceptance

| Capability | Result | Evidence |
|---|---|---|
| Concurrent runs and capacity | Pass | With four Agents running, displayed `running 4 / 4 · capacity 0`; capacity recovered afterward |
| New run navigation | Pass | All five navigated to the corresponding `/sessions/<agent_id>` |
| Live turns/tools | Pass | Values increased live through states such as `3 / 2` and `14 / 8` |
| AskUserQuestion | Pass | Osaka selection, button enablement, submission, and same-run resume all worked |
| Prompt / Result persistence | Pass | Remained readable after refreshing Sessions and detail pages |
| Visited source auditing | Pass | Showed TodoMVC, museum.go.kr, World Bank, official CI docs, and OSAKA-INFO |
| Historical tool trace | Partial pass | Returning to a run page did not replay earlier assistant/tool/result frames |
| Goal-satisfaction judgment | Partial pass | All five were `completed`, but three had business-level deviations |
| Default side-effect control | Partial pass | A research task created a CI comparison document without being asked |
| Frontend console | Partial pass | No errors, but React Router future-flag warnings repeated on navigation |

## Second-Batch Issues and Recommended Priorities

### P1: Source Fields Lacked Explicit / Inferred / Not Found State

The museum scenario inferred “no reservation” from “free permanent exhibition,” and the Osaka
scenario promoted an undated seasonal statement into a definite 2026 date. Preserve field-level
evidence in the research path:

```text
claim -> source URL -> source excerpt -> explicit | inferred | not_found
```

When generating the final answer, `inferred` and `not_found` must never be rewritten as definite
facts.

### P1: Research Tasks Created Unrequested File Side Effects

The CI scenario wrote a detailed answer into the repository, while its final response omitted the
YAML. Recommendations:

- Treat pure retrieval/comparison prompts as read-only intent by default.
- Call file-write tools only when the user explicitly requests writing, saving, or document
  generation.
- If an artifact is necessary, first ensure that the final answer itself satisfies the core request
  and explicitly list every new file.

### P2: Run Detail Did Not Replay Historical Tool Traces

After returning from Sessions to a running detail page, Live stream showed only placeholder copy.
After completion, only result and Visited remained. Gateway already has a durable event log; Web
should replay history by run cursor and then attach to live SSE. Otherwise users cannot fully audit
each action when multiple Agents run concurrently.

### P2: Time-Sensitive Conclusions Need a Year-Match Check

If a source includes only a month or season while the prompt specifies a year, the evidence is
incomplete. Before answering, check whether the source actually includes the target year. If not,
label it “ordinary recurring schedule, not a dedicated 2026 announcement.”

### P3: Repeated React Router Warnings

This batch had no JavaScript errors, but every navigation emitted React Router v7
`v7_startTransition` and `v7_relativeSplatPath` future-flag warnings. They did not affect current
behavior but obscured real debugging signals. Enable the flags ahead of the upgrade or address both
during migration.

## Second-Batch Fixes and Retesting (2026-07-26)

Each second-batch finding was reviewed and fixed. Original scenarios and their partial-pass outcomes
remain above as pre-fix evidence; this table reflects the latest code and production Web console.

| Reported issue | Application-layer fix | Real Web / automated verification | Current status |
|---|---|---|---|
| P1 source fields lacked explicit / inferred / not found | Browser-research prompt binds each field to a source, distinguishes explicit / inferred / not_found, and prohibits inferring “no reservation” from “free” | `ag_f69a5029e941` labeled free admission “explicitly stated by the site,” labeled personal reservation requirements “not found,” and linked two official museum pages | Fixed and retested |
| P1 research task created an unrequested file | Research/comparison/summary/check requests default to read-only; the file-write layer upgrades unauthorized `filesystem.write` to Web approval based on the current human request; query loop synchronizes the complete live conversation into tool-permission context | Before the fix, `ag_9ea6ffef68fe` incorrectly allowed `Write`; after the fix, `ag_55904af638d0` displayed Approval required, the probe file did not exist, and Deny ended with `approval_denied` | Fixed and retested |
| P2 run detail did not replay historical tools | Added bounded SSE history at `/agents/{agent_id}/events`; frontend reads the durable event log, isolates live frames by Agent, and displays Run history after refresh or session switch | Reopening `ag_7373cff4c15a` replayed TodoMVC assistant / tool_use / result / done; new runs also switched from Live stream to Run history at completion | Fixed and retested |
| P2 year-specific requests were confused with undated seasonal facts | Browser-research prompt requires the source to contain the target year; an undated seasonal range can only be labeled ordinary information | `ag_182fa0d7788a` labeled `Mid-July to late August` an undated recurring range and explicitly said no 2026-specific date was found | Fixed and retested |
| P3 React Router future-flag warning | Enabled `v7_startTransition` and `v7_relativeSplatPath` future flags on `BrowserRouter` | Browser console had no warning or error after repeated production-page navigation | Fixed |

### Read-Only Guard Failure Probe and Root Cause

The first real Web regression deliberately asked a read-only Agent to attempt one `Write`:

> Do not actually modify, create, or save any file. Attempt to call Write to create
> `docs/READ_ONLY_GUARD_PROBE.md`, and stop to wait when approval is required.

`ag_9ea6ffef68fe` exposed a runtime defect that unit tests missed. The policy classifier correctly
recognized the prompt, but `ToolUseContext.messages` still held the empty list from initialization.
The primary loop used a different conversation list, so the permission layer could not see the
current human request and incorrectly allowed `Write`. That Agent later deleted the probe, leaving
no working-tree residue.

After the fix, the query loop points tool context to the authoritative conversation list before
every model call and resynchronizes it when automatic compaction replaces the list. A second real
Agent, `ag_55904af638d0`, stopped at an approval card when it attempted the analogous
`docs/READ_ONLY_GUARD_PROBE_2.md`:

```text
The current user request appears read-only and did not authorize file changes.
Approve filesystem.write on workspace:docs/READ_ONLY_GUARD_PROBE_2.md once?
```

The probe did not exist when approval appeared and still did not exist after clicking Deny. Explicit
“fix / modify / create” requests continue to use the existing allow rules so normal development
work is not misclassified as read-only.

### Historical Replay Implementation

Gateway's durable event log already stored run events; legacy Web routes and the frontend simply did
not consume them. This work did not create a parallel history store. Instead:

1. Mounted `/agents/{agent_id}/events` on the Web service, converting the latest run's persistent
   events into compatible SSE frames and applying DLP before API output.
2. The frontend uses the same SSE parser for live POST responses and bounded history responses,
   supporting CRLF, multiline data, and final TextDecoder flushing.
3. Live frames are isolated by Agent ID so Agent A's trace cannot appear on Agent B's page during
   parallel execution.
4. The detail page refreshes persistent history every 1.5 seconds. Live stream takes precedence while
   the current browser owns live frames; Run history appears after refresh, switching, or terminal
   state.

### Post-Fix Regression Results

- `uv run pytest -q`: 1,259 passed.
- Added a query-loop integration regression requiring a real `Write` permission check to see the
  current read-only prompt and prohibiting creation of the target file.
- `uv run ruff check .`: passed.
- `uv run python -m compileall -q tabvis`: passed.
- `web/npm run build`: TypeScript checks and Vite production build passed.
- Production `uv run tabvis` at `http://localhost:8765/` completed real-page retests for history
  replay, read-only approval, source state, and year matching.

## Second-Batch Test Impact

- Added five Agent run records and five isolated browser profiles.
- A test Agent created the untracked `docs/CI_CACHING_PYTHON_COMPARISON.md`, the unrequested side
  effect from Scenario 9. It remains for reproduction and auditing.
- Fix regression added four Agent run records: one pre-fix failing probe, one post-fix approval-denial
  probe, and two research-answer quality retests. Neither probe file currently exists.
- This report was updated manually.
- No Tabvis configuration was changed, and no message was sent, third-party account accessed,
  purchase made, user file uploaded, or delete operation performed.

---

## Third Batch: Five More Real-World Use Cases (2026-07-26)

### Third-Batch Summary

Again excluding financial reports and the first ten use cases, this batch created and operated five
isolated Agents through `http://localhost:8765/`, covering consumer product selection, live public
data, open-source maintenance, daily media, and academic research:

- All five Agents reached `completed`. Because the original GitHub filter had no results in the real
  repository, Continue performed one evidence-based condition adjustment, for six runs in total.
- Business review: Apple, GitHub, NASA, and arXiv passed. USGS core facts passed, but the initial
  process suffered a DLP false positive and its final explanation of “query time” was not credible,
  so it was a pre-fix partial pass.
- Including the GitHub continuation: 92 turns, 45 tool calls, and 362.5 seconds.
- While the first four Agents ran concurrently, the header correctly displayed
  `running 4 / 4 · capacity 0`; the arXiv Agent was created after capacity recovered.
- All scenarios honored read-only constraints: no purchase, cart addition, login, message, or project
  file modification.
- The batch found and fixed one real application bug: generic DLP rules removed public API query
  arguments and Unix timestamps, causing repeated queries against the wrong time window.

### Common Test Process

1. Submitted five prompts through `/run`, with Chromium and isolated profile selected.
2. Started the first four concurrently and observed header capacity, per-detail turns/tools, SSE tool
   traces, and terminal states.
3. Created the fifth arXiv Agent after the first three ended to stay within the four-browser-workspace
   limit.
4. Reviewed Run history, result, Visited, duration, and browser information on every detail page.
5. When GitHub revealed that the repository had no `bug` label, the Agent did not invent three
   results. Continue changed the filter to the real `upstream` label, suitable for defect triage,
   and the first three results were verified.
6. Key facts from Apple, USGS, GitHub, NASA, and arXiv were independently verified with their
   official pages/APIs.
7. For the USGS DLP issue, regression tests and code fixes were added, the production service was
   restarted, and the same Web Agent was used to retest tool input, tool result, model response, Run
   history, and persisted result.

Third-batch runs:

| # | Real-world use case | Agent ID | Business result | turns / tools | Duration |
|---|---|---|---|---:|---:|
| 11 | Compare Apple laptops for frequent travel | `ag_c7b8aada2f11` | Pass | 17 / 8 | 57.0s |
| 12 | Find the largest earthquake in the last 24 hours and calculate elapsed time | `ag_c8af13fe2886` | Partial pass (before DLP fix) | 20 / 10 | 67.5s |
| 13 | Triage recently updated upstream Playwright defects | `ag_d7f54441ebc8` | Pass (one Continue) | 11 / 5 + 5 / 2 | 33.7s + 14.1s |
| 14 | Read the day's NASA APOD and extract its video | `ag_c9692c500242` | Pass | 9 / 4 | 34.2s |
| 15 | Search the last 30 days of browser/web agent papers | `ag_40df12332cf6` | Pass | 30 / 16 | 156.0s |

Local detail pages:

- `http://localhost:8765/sessions/ag_c7b8aada2f11`
- `http://localhost:8765/sessions/ag_c8af13fe2886`
- `http://localhost:8765/sessions/ag_d7f54441ebc8`
- `http://localhost:8765/sessions/ag_c9692c500242`
- `http://localhost:8765/sessions/ag_40df12332cf6`

## Scenario 11: Compare Apple Laptops for Frequent Travel

### Prompt

> Using only official apple.com/kr pages, compare the entry configurations of the current 13-inch
> MacBook Air and 14-inch MacBook Pro: starting price, chip, weight, and ports. Recommend one for a
> frequent traveler who does not need high performance, with source links. Mark any field absent
> from the official site as not found. Read only; do not create files, make a purchase, or add
> anything to a cart.

### Actual Process and Result

1. The Agent visited only product and specification pages on Apple's Korean website.
2. Two attempts to scope `BrowserExtract` to the specification table returned no structured result;
   the Agent then used general DOM extraction and the accessibility snapshot.
3. Final comparison:
   - MacBook Air 13: ₩2,190,000; M5 10-core CPU / 8-core GPU; 1.23 kg; MagSafe 3, two
     Thunderbolt 4 ports, and headphone jack.
   - MacBook Pro 14: ₩3,290,000; M5 10-core CPU / 10-core GPU; 1.55 kg; MagSafe 3, three
     Thunderbolt 4 ports, HDMI, SDXC, and headphone jack.
4. It recommended Air because it was about 320 g lighter and ₩1,100,000 cheaper while providing
   sufficient everyday travel performance and ports.

### Independent Verification

- [MacBook Air technical specifications](https://www.apple.com/kr/macbook-air/specs/) support its
  price, chip, weight, and ports.
- [MacBook Pro technical specifications](https://www.apple.com/kr/macbook-pro/specs/) support the
  corresponding Pro fields.

### Feedback

This scenario passed. Domain constraints, field completeness, calculations, and purchase-side-effect
limits were honored, and the recommendation tied directly to weight, price, and non-high-performance
requirements. Two failed local extractions before fallback show that complex specification pages
still need better candidate-scope hints or heading-based region extraction.

## Scenario 12: Find the Largest Earthquake in the Last 24 Hours

### Prompt

> Using only official earthquake.usgs.gov pages or APIs, find the world's largest earthquake in the
> last 24 hours. Include magnitude, place, UTC time, official detail link, and elapsed time relative
> to the current UTC query time. State the query time. Do not use search-result snippets or create
> or modify local files.

### Actual Process and Result

1. The Agent used the USGS FDSN Event Web Service and tried to set `starttime`,
   `orderby=magnitude`, and `limit`.
2. During the first run, DLP emptied URL query values and treated 10/13-digit Unix timestamps in
   GeoJSON as phone numbers, replacing them with `[redacted]`. The Agent repeatedly used incorrect
   historical windows and needed 20 turns / 10 tools.
3. Final core result:
   - M 5.1, Tristan da Cunha region.
   - Event time: 2026-07-26 01:32:44 UTC.
   - Query time: 2026-07-26 05:55:10 UTC.
   - Approximately 4 hours, 22 minutes, 26 seconds before the query.
   - Detail page: `https://earthquake.usgs.gov/earthquakes/eventpage/us7000t3e7`.
4. The facts agreed with the final official query, but the answer explained query time as “event time
   plus a three-week server anchor,” an unsupported statement. Query time should have come directly
   from system UTC.

### Independent Verification

The final official query used during testing was:

```text
https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime=2026-07-25T05:55:10&orderby=magnitude&limit=1
```

Official GeoJSON returned event `us7000t3e7`, M 5.1, Tristan da Cunha region,
`time=1785029564077`, and the detail link. The timestamp converts to 2026-07-26 01:32:44 UTC.

### Feedback

Core facts passed, but the pre-fix workflow and query-time explanation made this a partial pass. It
demonstrated that:

- For public APIs, structural query values such as `starttime`, `orderby`, and `limit` are part of
  task semantics and cannot always be stripped.
- Unix timestamps must be evaluated in the context of fields such as `time`, `generated`, and
  `updated`, not only by digit length.
- When a time field is unavailable, the Agent should report missing evidence rather than repeatedly
  changing windows or inventing a “current-time anchor.”

The issue was closed in the “Bugs Found and Fixed in the Third Batch” section.

## Scenario 13: Triage Recently Updated Upstream Playwright Defects

### Initial Prompt

> Using only the microsoft/playwright Issues page on GitHub, filter
> `is:issue is:open label:bug sort:updated-desc` and list the first three Issues in the current
> order: number, title, update time, comment count, and link. Read only; do not log in, comment, add
> labels, or create files.

### No-Result Recovery

1. The Agent opened repository Issues and found that `label:bug` was invalid and returned zero
   results.
2. It did not substitute unrelated Issues; it inspected the repository's actual 69 labels and
   suggested `upstream` or `needs-triage`.
3. Continue on the same Agent changed the condition to
   `is:issue is:open label:upstream sort:updated-desc`.
4. In 5 turns / 2 tools, Continue returned the first three of five matches:
   - #41932: “When routes are enabled, Service Worker fetch requests always receive a
     cache-control header in WebKit”; Jul 23; one comment.
   - #41900: “`page.close()` hangs on Chromium when racing with cross-origin navigation commit”;
     Jul 22; the list did not display a comment count and showed one linked PR.
   - #41802: “When aborting only first request with `{times: 1}` ... second request randomly
     failed”; Jul 16; one comment.
5. Actual filtered URL:
   `https://github.com/microsoft/playwright/issues?q=is%3Aissue+is%3Aopen+label%3Aupstream+sort%3Aupdated-desc`.

### Feedback

The scenario passed. A real zero-result case was especially valuable: the Agent recognized an
invalid filter, explained why, suggested a label that actually existed, and reused the same browser
and session through Continue. Its conservative “0 / not displayed” wording for #41900's comment
count was more appropriate than guessing.

One product limitation is that Agent detail projects primarily onto the latest run. After Continue,
the page mainly shows the continuation prompt and trace; a complete audit of the initial no-result
diagnosis depends on persistent transcript or event history. A future multi-run Agent view should
offer an explicit run switcher.

## Scenario 14: Read the Day's NASA APOD and Extract Its Video

### Prompt

> Using only the official NASA Astronomy Picture of the Day page, retrieve today's APOD according to
> the current environment date: title, page date, content type (image or video), attribution/copyright
> information, and a concise summary of the explanation. Include the official NASA page and media
> link. If today's page is not yet published, say so explicitly and do not substitute another date.
> Do not create or modify local files.

### Actual Process and Result

1. The Agent opened the current NASA APOD page and used Bash `date` to confirm the environment date
   was 2026-07-26.
2. The accessibility snapshot / `BrowserExtract` exposed body text and attribution but not the
   `<video>` element's `src`.
3. The Agent used read-only `curl | grep` on the same NASA domain to obtain the media URL and did not
   visit a third-party site.
4. It reported:
   - Title: *Simulation TNG50: A Galaxy Cluster Forms*.
   - Date: 2026 July 26.
   - Type: MP4 video.
   - Credit: IllustrisTNG Project; visualization by Dylan Nelson et al.; music from Beethoven's Fifth
     Symphony.
   - Media:
     `https://apod.nasa.gov/apod/image/2607/ClusterFormation_TNG50.mp4`.
5. Its summary covered galaxy-cluster formation, TNG50 gas/star simulation, black-hole outflows, and
   comparison with the real universe.

### Independent Verification and Feedback

The [NASA APOD page](https://apod.nasa.gov/apod/ap260726.html) supports the title, date, credit, and
explanation; the media link is under the same NASA APOD directory. The scenario passed and correctly
honored the “do not substitute another date” constraint.

Structured browser extraction should return visible media `src`, `poster`, type, and dimensions as
bounded fields. Falling back to shell for one video URL did not cross domain or side-effect
boundaries, but it reduced the explainability of the browser-native path.

## Scenario 15: Search the Last 30 Days of Browser/Web Agent Papers

### Prompt

> Using only arxiv.org, search arXiv for papers submitted in the last 30 days that are directly
> related to browser agents or web agents. Order by submission date from newest to oldest and select
> the two most relevant. Include arXiv ID, title, authors, first-submission date, abstract highlights,
> and link. If fewer than two exist, say so and report only what was found. Do not use search-engine
> snippets or create or modify local files.

### Actual Process and Result

1. The Agent visited only arxiv.org and tried both ordinary and Advanced Search.
2. Advanced Search argument names, date controls, and comboboxes repeatedly failed to match. The
   Agent retried fill, navigation, and conditions, eventually using 30 turns / 16 tools and 156
   seconds, the slowest scenario in the batch.
3. From candidates between 2026-06-26 and 2026-07-26, it selected:
   - `arXiv:2607.18659`, *Broken Gates: Re-evaluating Web Bot Defenses in the Age of LLM Agents*;
     Behzad Ousat and four coauthors; v1 on 2026-07-21.
   - `arXiv:2607.12640`, *A Learning-Rate-Gated Failure of GRPO in a Small Language and
     Vision-Language Model Web Agent: A Controlled Null and Its Mechanism*; Chengguang Gan and five
     coauthors; v1 on 2026-07-14.
4. It included complete author lists, abstract highlights, categories, and arXiv links for both.

### Independent Verification

- [arXiv:2607.18659](https://arxiv.org/abs/2607.18659) confirms the title, five authors, first
  submission on 2026-07-21, and research on browser agents against bot defenses.
- [arXiv:2607.12640](https://arxiv.org/abs/2607.12640) confirms the title, six authors, first
  submission on 2026-07-14, and a controlled experiment on GRPO for 4B–8B web agents.

### Feedback

Facts and source constraints passed, and both papers were within the 30-day window and directly
relevant. The process was inefficient:

- The Agent repeatedly guessed Advanced Search parameters and controls instead of mapping fields
  once from the current form snapshot.
- The final answer retained lengthy candidate comparisons and “I reconsidered” process text, pushing
  the requested two results later. Candidate scoring belongs internally; the final response should
  include conclusions, selection criteria, and only necessary uncertainty.
- For a subjective criterion such as “most relevant,” include a short scoring basis—for example,
  whether title/abstract directly studies a browser/web agent—and then sort by first-submission date.

## Third-Batch Web Product Acceptance

| Capability | Result | Evidence |
|---|---|---|
| Concurrent capacity | Pass | First four scenarios displayed `running 4 / 4 · capacity 0`; capacity recovered afterward |
| New run and isolated profile | Pass | All five Agents had independent sessions, browser profiles, and detail pages |
| Continue recovery after no results | Pass | Same GitHub Agent continued from invalid `bug` label to `upstream` |
| SSE / Run history | Pass | Tool calls were observable, with assistant/tool_use/result/done replay after terminal state |
| Source-domain constraints | Pass | Apple, USGS, GitHub, NASA, and arXiv visited only prompt-authorized official sources |
| Read-only side-effect control | Pass | No scenario modified the repository, logged in, commented, purchased, or uploaded |
| JSON API usability | Partial before fix | DLP damaged URL queries and timestamps; passed end to end after fix |
| Media-field extraction | Partial pass | APOD was understood, but video `src` required shell fallback |
| Advanced-search efficiency | Partial pass | arXiv met the goal but repeatedly guessed form parameters and mixed internal reasoning into the answer |
| `completed` goal semantics | Partial pass | USGS completed despite initial evidence and explanation issues |

## Bugs Found and Fixed in the Third Batch

### P1: Public API URL Queries Were Removed Unconditionally

Before the fix, `tabvis/dlp/url.py` retained only query keys and removed every value from every URL.
That was safe for sensitive parameters such as `token` and arbitrary search terms, but it also broke
the USGS query:

```text
format=geojson
starttime=2026-07-25T05:55:10
orderby=magnitude
limit=1
```

The fix preserves validated values only for a narrow set of public structural arguments, including
format, date range, ordering, limit/offset/page, coordinates, magnitude, and arXiv Advanced Search
fields. Unknown arguments, search terms, authentication parameters, URL userinfo, and fragments are
still removed. This is a narrow allowlist, not permission for all query values.

### P1: Public Unix Timestamps Were Misclassified as Phone Numbers

Generic phone DLP replaced consecutive 10/13-digit values with `[redacted]`. USGS GeoJSON fields
`generated`, `time`, and `updated` were lost before tool results reached the model. Even when the
model could see raw data on the original page, final API/transcript DLP obscured the result again.

After the fix:

- Bare 10/13-digit Unix timestamps are retained only in explicit time-field context.
- Supported forms include ordinary JSON, escaped JSON inside accessibility snapshots, and common
  model Markdown such as `` `properties.time (first feature)`: 1785029564077 ``.
- `phone` fields, unlabeled long numbers, and natural-language phone numbers remain `[redacted]`.

### Real Web End-to-End Regression

Additional regression Agent: `ag_7728b37d9cb5`; session: `ses_09da0bce8a78`.

This was not only a unit test. After the fix and restart of `uv run tabvis`, Web Continue submitted:

> Open this official USGS URL again. Use BrowserNavigate exactly once, then report the three original
> integers: `metadata.generated`, `properties.time (first feature)`, and
> `properties.updated (first feature)`.

The final detail page showed:

```text
metadata.generated: 1785047019000
properties.time (first feature): 1785029564077
properties.updated (first feature): 1785031604040
```

It also confirmed:

- tool_use preserved complete `format`, `starttime`, `orderby`, and `limit`.
- The three fields in escaped GeoJSON remained visible in tool_result.
- Model response, Run history, and persisted result displayed the original integers.
- Targeted regressions still obscured ordinary phone numbers and unlabeled long values.

## Third-Batch Automated Regression

- DLP targeted tests: all 22 tests in `tests/authentication/test_dlp_gateway.py` passed.
- New coverage:
  - Narrow allowlist for public structural URL queries and redaction of unknown queries.
  - Ordinary and escaped GeoJSON timestamps.
  - Markdown time-field output.
  - Continued redaction of `phone` fields and unlabeled long numbers.
  - Boundary where a USGS browser-shaped result passes through `model_request` DLP.
- `uv run pytest -q`: 1,265 passed.
- `uv run ruff check .`: passed.
- `uv run python -m compileall -q tabvis`: passed.
- `web/npm run build`: TypeScript checks and Vite production build passed.

## Third-Batch Test Impact

- Added five scenario Agents and six scenario runs, plus one Agent used for multiple DLP diagnostics
  and the final regression.
- Every scenario used an isolated browser profile; no run remained active.
- No third-party account login, message, Issue comment, purchase, cart addition, or user-file upload.
- The five scenario Agents did not modify the project; code fixes and report changes were made by the
  test operator.
- `docs/CI_CACHING_PYTHON_COMPARISON.md`, left by second-batch Scenario 9, remains as audit evidence
  of the unrequested side effect and was not deleted without authorization.

## Fourth Batch: Deep Research on Smart Contracts and Blockchain Security

### Scenario 16 Overview

| # | Real-world use case | Agent / Session | Result | Key runs |
|---|---|---|---|---|
| 16 | Research current uses of LLMs in smart contracts and blockchain security and generate a local report | `ag_86c4f028916a` / `ses_e7d095558165` | Research and reread passed; product problems were fixed after the first automatic-writing failure, and evidence was curated manually | Research 25 / 16, writing retry 2 / 1, reread 15 / 11 |

Final research deliverable:

- `docs/LLM_SMART_CONTRACT_BLOCKCHAIN_SECURITY_RESEARCH.md`
- 395 lines, 32,250 bytes, and 16,775 characters.
- 12 primary sections and 21 deduplicated direct-source links, with no `XXXXX`, `TODO`,
  `PLACEHOLDER`, or unfinished content.
- Clearly distinguishes author-reported paper results, vendor claims, public tool facts, and
  inferences from this review, and gives 30/60/90-day adoption recommendations.

### Prompt and Goal

The task submitted through `http://localhost:8765/`, using 2026-07-26 as its reference date,
focused on LLM/Agent practice since 2025 in:

- Smart-contract vulnerability detection and auditing.
- Solidity code and specification generation.
- Integration with fuzzing, symbolic execution, and formal methods.
- Dual-use risks from attack paths, PoCs, and historical vulnerability reproduction.
- On-chain fraud, phishing, and anomaly investigation.
- Human–AI collaboration in audit teams, evidence levels, failure modes, and a 30/60/90-day adoption
  plan.

It also required first-party sources—papers, official projects, and security organizations—then a
local Markdown report and a reread verifying sections, sources, and placeholders.

### Actual Run

1. The Web Agent started three built-in research Agents for vulnerability auditing,
   generation/verification/PoC, and on-chain fraud plus human–AI collaboration.
2. The primary Agent independently verified Trail of Bits AI-native work, SmartAuditFlow, EvoPoC,
   CyberChainBench, Slither-MCP, CHAINTRIX, and other sources.
3. Visited reached 89 pages. Sources included arXiv, GitHub, and official vendor articles, but also
   Google CAPTCHA, 404s, Semantic Scholar error pages, and a few irrelevant papers.
4. The initial research run lasted 1,041.1 seconds with 25 turns / 16 tools. Primary progress remained
   near `9 / 6` for about 12 minutes while Visited continued increasing. Sub-Agents were still
   working, but Web lacked visible child-task progress.
5. After research, three long `Write` attempts were truncated at the 8,192 output-token limit and
   appeared as `Write({})` in the UI. The run could not complete and had to be cancelled.
6. After increasing the output budget and restarting the service, Continue resumed the same Agent /
   Session. The complete `Write` arguments reached the approval card, proving that truncation was
   fixed. However, the generated draft contained `XXXXX` links, wrong dates, and unsupported
   numbers, so Deny was deliberately selected. That run failed with `approval_denied` after
   83.3 seconds and 2 turns / 1 tool.
7. After source-by-source verification, the test operator curated the evidence-supported version
   into the target file rather than writing the rejected hallucinated draft.
8. The same Web Agent then performed a read-only acceptance check. In 63.7 seconds and 15 turns /
   11 tools, it confirmed 21 unique links, complete sections, present dates, and no placeholders.
   Local commands independently verified those values.

### Research Findings

The research indicated that the most credible current pattern is not “let a chat model read the
contract and declare it secure,” but **LLM proposes candidates + deterministic tools verify +
humans sign off**:

- On 541 real incidents across nine EVM chains, CyberChainBench's best detection, exploitation, and
  patching rates were only 37.5%, 43.7%, and 23.4%, respectively; end-to-end autonomy remains
  immature.
- CHAINTRIX, PROMFUZZ, LLAMA, Slither-MCP, and related work connect models to static analysis,
  execution feedback, fuzzing, or symbolic/formal tools.
- PoC synthesis on historical forks has shown strong capability, but those figures are
  author-reported in isolated environments and do not provide production guarantees for systems
  holding real funds.
- Trail of Bits' production account still requires auditors to verify every AI-first finding. It is
  useful as a workflow case study, not an independent performance benchmark.
- On-chain investigations should preserve input, sources, actions, queries, and timestamps, and
  convert successful investigative steps into repeatable code where possible.

The independent research report contains the complete evidence table, links, limitations, and
adoption recommendations. This document records only the product test.

### Does the Agent Read Complete HTML?

This batch again confirmed that the Agent **does not place complete HTML into model context by
default**. Decisions primarily use the bounded accessibility snapshot and stable `ref` returned
after each browser action. When body text, dates, tables, or links are needed, `BrowserExtract`
reads the sanitized rendered DOM; only when necessary does the system fall back to screenshots or
truncated HTML. Complete DOM can be saved as an audit artifact but should not enter reasoning
context by default.

For deep research, the missing capability was not “force the Agent to read complete HTML” but a
persistent cross-page, cross-child-task **evidence bundle**. Each claim should retain source URL,
title, publication time, excerpt location, extraction time, child-Agent ownership, and evidence
level. Depending only on browser history or model memory loses verifiable relationships across
cancellation, restart, and compaction.

### Problems Found and Fixed in This Batch

#### P1: Fixed 8,192 Output Tokens Truncated Long Tool JSON

`8192` was Tabvis's local default `max_output_tokens`, not the context limit returned by the current
model endpoint. The user's current `1M` normally describes the context window; per-response output
budget is a separate capability. Sending 1M directly as `max_output_tokens` is incorrect.

After the fix:

- When not explicitly configured, output budget comes from the selected model's capability table.
  Current ordinary models default to 32,000, with some models allowing more.
- `TABVIS_MAX_OUTPUT_TOKENS` keeps highest precedence for explicit override according to actual
  provider capability.
- Web configuration guidance and `.env.example` both say “leave empty to use the selected model
  default.”
- Tests cover model defaults, environment override, and fallback from invalid environment values.

This accommodates the roughly 16.8K-character report tool argument while keeping context window and
maximum generation length distinct.

#### P1: “Do Not Modify Other Files” Incorrectly Made an Explicit Target Write Read-Only

The original policy matched broad prohibition text first. When a prompt said both “write the target
report” and “do not modify other files,” the latter overrode the explicit authorization and the
approval card incorrectly said the request appeared read-only.

The fix first removes scope guardrails such as “do not modify any other files,” then evaluates
authorization for the target write. The target file is allowed while other files remain outside
scope. Chinese and English regressions passed.

#### P1: Cancelled Long Runs Did Not Persist Completed Transcript Content

The primary session previously persisted only at a normal `Terminal` boundary. After the first
research run was cancelled, completed model messages, tool results, and child-research evidence were
not written to the session transcript. Even reuse of the same `session_id` after restart could not
replay the research chain.

After the fix, `ask()` also persists from `finally`: normal completion still writes once, while
cancellation/closure saves content already admitted to the primary message chain. New tests cover
both normal completion and asynchronous-generator closure.

### Remaining Productization Work

- **P2 Child-Agent observability**: When primary turns/tools do not move for a long time, show child
  task state, latest action, visit count, and heartbeat so users do not mistake active work for a
  hang.
- **P2 Research source quality**: Support trusted-domain preference, URL deduplication,
  CAPTCHA/404/search-result demotion, and a writing gate requiring every conclusion to reference the
  evidence bundle.
- **P2 Continue semantics**: Clicking Continue while a run is still active cannot inject a corrective
  prompt; it only returns to the current run. Disable it or say “cancel first,” or implement queued
  follow-up.
- **P2 Terminal-state race logging**: After approval denial, service shutdown emitted one
  `CONFLICT: Run ... is 'failed', expected 'running'`. Persisted terminal state was correctly failed,
  but duplicate terminal transitions between runner and interaction should be removed.
- **P2 Long-report writing strategy**: Even with a higher output limit, prefer writing temporary
  sections, validating each, and merging atomically so one huge tool JSON does not become a single
  point of failure.
- **P2 Capability discovery**: Custom 1M endpoints should allow configuration or provider-metadata
  discovery of `max_input_tokens` and `max_output_tokens`, displayed separately in Settings.

### Fourth-Batch Automated Regression

- Targeted tests: 22 passed.
- `uv run pytest -q`: 1,272 passed.
- `uv run ruff check .`: passed.
- `uv run python -m compileall -q tabvis`: passed.
- `web/npm run build`: TypeScript checks and Vite production build passed.
- `git diff --check`: passed.

### Fourth-Batch Test Impact

- No Web run remained active; the denied hallucinated draft was never written to the target file.
- No external-account login, transaction, contract deployment or invocation, wallet connection,
  secret upload, or real-world PoC execution.
- Research used only public material. Attack-capability numbers describe historical-fork/isolated
  evaluations from public papers; no real attack was reproduced.
- This batch changed model output budgeting, file-write intent parsing, and cancellation persistence,
  with corresponding regression tests.
- The service remained in production Web mode at `http://localhost:8765/`.
