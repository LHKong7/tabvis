# Tabvis Web Console Real-User Scenario Test Report

- Test date: 2026-07-25
- Test URL: `http://localhost:8765/`
- Test method: Actual browser clicks, filtering, navigation, refreshes, copy actions, and submitting settings without changes
- Desktop environment: Default browser viewport
- Mobile environment: `390 × 844`
- Test data: Four existing historical sessions in the service (three completed and one cancelled)

## Executive Summary

This round covered ten real-user scenarios:

- 5 passed
- 4 partially passed
- 1 failed

Core capabilities such as the dashboard, session filtering, browser status, reading and saving
unchanged settings, command copying, and SPA deep-link refreshes worked correctly. However, the
essential **New run / Continue workflow renders a blank screen**, which means users cannot create or
continue tasks from the Web console with the current data. This should be treated as the
highest-priority issue.

## Scenario Results

| # | User scenario | Actions performed | Result | Feedback |
|---|---|---|---|---|
| 1 | Open the console and inspect the system overview | Opened the home page and waited for the health poll to complete | Pass | Correctly displayed running, capacity, sessions, open browsers, and the active browser engine. Updating from `connecting…` to the actual state takes approximately one polling cycle. |
| 2 | View and filter historical sessions | Opened Sessions and clicked `completed 3` | Pass | Correctly filtered the list from four entries to three. The active filter and status counts were clear and accurate. |
| 3 | Inspect a completed session | Opened a completed session | Partial pass | Displayed status, agent/session IDs, turn/tool counts, and duration. However, current historical records do not display the prompt or result, and Live stream is empty, so users cannot review the task or final answer. |
| 4 | Create a new task or continue a historical session | Clicked New run and, separately, Continue on a session detail page | **Fail** | Both entry points navigated to `/run` and then rendered a blank screen. Console error: `TypeError: Cannot read properties of undefined (reading 'slice')`. |
| 5 | Inspect browser drivers and runtime status | Opened Browser and reviewed the active engine, driver state, and open browsers | Pass | Correctly displayed the active engine, kernel, connection mode, installation state, and Open browsers. The information was complete, although the list of 21 drivers and advanced options was long. |
| 6 | Review and save settings | Opened Settings, confirmed that configuration loaded, and clicked Save & apply without changing any values | Pass | Settings loaded correctly. Saving without changes did not rewrite configuration and displayed `applied live — no restart needed`. The page is very long and lacks group navigation, search, or collapsible sections. |
| 7 | Copy a command from the installation instructions | Opened Setup and clicked copy in the Install section | Partial pass | The button correctly changed from copy to copied. Startup instructions included both `uv run tabvis` and `--serve`. However, the security note still states that the service has “no authentication,” which conflicts with the current implementation that requires a token when binding beyond loopback. |
| 8 | Open and refresh a deep session link | Opened `/sessions/<id>` and refreshed the page | Pass | The same session detail page was restored before and after refresh, confirming that the production SPA fallback works correctly. |
| 9 | Visit an unknown frontend route | Opened `/not-a-real-page` | Partial pass | The application silently displayed Dashboard while leaving the invalid path in the address bar. There was no 404, redirect, or “page not found” message, which could make users believe the link is valid. |
| 10 | Use the console at mobile width | Set the viewport to `390 × 844` and inspected the home page and navigation | Partial pass | There was no horizontal overflow, and content width and top navigation adapted correctly. However, after CSS hides navigation text, accessible names are reduced to symbols such as `◧`, `＋`, and `≣`, which are unintelligible to screen readers and voice control. |

## Main Issues and Priorities

### P0: New run / Continue Renders a Blank Screen, Blocking the Core Workflow

Reproduction steps:

1. Ensure that the service contains at least one historical Agent record with a missing `prompt`.
2. Click New run in the sidebar, or click Continue on a historical session detail page.
3. The page navigates to `/run` and becomes blank.

Browser error:

```text
TypeError: Cannot read properties of undefined (reading 'slice')
```

The direct cause is in `web/src/components/NewRun.tsx`:

```tsx
{agents.map((a) => (
  <option key={a.agent_id} value={a.agent_id}>
    Continue {a.agent_id} · {a.status} · {a.prompt.slice(0, 40)}
  </option>
))}
```

At least one record returned by `/agents` has no `prompt`; the list page also shows a blank summary
for that record. The frontend nevertheless treats `prompt` as a required string.

Recommendations:

1. Add immediate frontend tolerance: `(a.prompt || 'No prompt').slice(0, 40)`.
2. Ensure that the API/compatibility projection always returns `prompt` and `result` as strings,
   rather than omitting them or returning `null`.
3. Add an Error Boundary to `/run` so one malformed record cannot blank the entire page.
4. Add regression tests for historical records with missing prompts or results.

Acceptance criteria:

- `/run` remains available when any field is missing from a historical record.
- A new agent can be submitted.
- A target Agent can be selected and submitted through Continue.
- The page produces no uncaught exception or blank screen.

### P1: Completed Session Results Cannot Be Reviewed Effectively

The detail page only displays `frames` for a run that is currently held in memory. Live stream is
always empty after a refresh or when opening a historical session. If the compatibility record also
lacks `result`, only metadata remains. From a real user's perspective, “completed but the answer is
unavailable” is equivalent to losing the task result.

Relevant implementation:

- `web/src/pages/SessionDetailPage.tsx`: passes an empty array to Stream for historical sessions.
- `web/src/components/Detail.tsx`: displays a result only when `agent.result` exists.

The service should expose a persistent event/final-result read API. The detail page should load
historical events or, at minimum, always display the final answer, prompt, and error.

### P1: Setup Security Guidance Conflicts with the Actual Authentication Policy

Setup currently says that the service has “no authentication” and that non-local deployment
requires an authenticating proxy. The server already implements the following:

- Authentication is automatically required for non-loopback addresses.
- Startup is rejected when `TABVIS_SERVER_ADMIN_TOKEN` is not configured.
- Administrative requests support Bearer Token authentication.

The relevant implementation is in `tabvis/browser/server_auth.py`. Outdated guidance may cause users
to misjudge deployment risk or build a redundant authentication layer.

The displayed guidance should adapt to the bind host and authentication state:

- Loopback: reachable only from the local machine; no token required by default.
- Non-loopback: `TABVIS_SERVER_ADMIN_TOKEN` is mandatory; include an example request header.
- Reverse proxy: present it as an additional hardening option, not the only authentication method.

### P2: Mobile Navigation Lacks Accessible Names

At small widths, `web/src/index.css` hides all text in `.nav-item` except the icon, while `NavLink`
has no `aria-label`. In testing, each accessible name became a single symbol.

Add `aria-label={n.label}` to each navigation link and retain `aria-current="page"` for the active
page.

### P2: Unknown Frontend Routes Silently Display Dashboard

The wildcard route in `web/src/App.tsx` directly renders `<Dashboard />` without redirecting or
showing an error.

Choose one of the following:

- Render an explicit Not Found page with a button that returns to Dashboard.
- Use `<Navigate to="/" replace />` so the address bar is also restored to `/`.

### P2: Browser / Settings Has Excessive Information Density

Browser displays 21 drivers and many advanced stealth settings at once. Settings displays every
Model, Browser, Stealth, Server, OCR, Artifacts, and Project field on one page. The feature set is
complete, but new users will struggle to locate the setting they need.

Recommendations:

- Add settings search.
- Use collapsible groups or a left-side anchor table of contents.
- Collapse fields unrelated to the active engine by default.
- Give each `Download` button a specific name, such as `Download CloakBrowser`.

## Positive Feedback

- Home-page health and capacity information is concise and updates correctly after polling.
- Session status filters and counts are intuitive and responsive.
- The production build supports deep-link refreshes with reliable SPA fallback behavior.
- Browser clearly distinguishes the active engine, kernel, connection, and installation status.
- Secret fields use a write-only design and do not return complete credentials to the page.
- Settings submits only changed fields; saving without changes does not accidentally persist defaults.
- Setup copy buttons work even on a local HTTP page.
- There is no horizontal scrolling at `390 × 844`; the basic responsive layout works.

## Recommended Fix Order

1. Fix the `/run` blank screen and add an Error Boundary.
2. Restore prompt, result, and event replay for historical sessions.
3. Update Setup authentication guidance.
4. Fix accessible names in mobile navigation.
5. Add explicit Not Found behavior.
6. Improve the information architecture of Browser and Settings.

## Test Impact

This round did not install drivers, switch browser engines, cancel or quit Agents, or modify user
settings. The only submitted action was saving Settings without changes; the frontend detected no
differences and returned immediately. One local clipboard-copy action was also performed. Because
New run rendered a blank screen, no new Agent task was created during this round.
