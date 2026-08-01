# Secure Credential Injection and Automated Authentication Design

> Status: Draft v0.1
> Date: 2026-07-24
> Audience: Tabvis maintainers, security engineers, and Browser Runtime and Gateway implementers
> Scope: Secure resolution, injection, verification, storage, destruction, and auditing of
> usernames/passwords, TOTP, and authenticated sessions
> Related documents: `docs/AGENT_GATEWAY_DESIGN.md`, `docs/DATA_MODEL.md`

---

## 0. Document Conventions

This is an implementation design, not a product introduction. Normative terms have the following
meanings:

- **MUST**: A security or correctness requirement that cannot be omitted.
- **SHOULD**: A default requirement that may be deviated from only when the reason is recorded.
- **MAY**: An optional extension.

The document uses these implementation-state markers:

| Marker | Meaning |
|---|---|
| **Current** | Already implemented in the current repository |
| **Target** | Final state required by this design |
| **Transition** | Temporary approach for migrating from the current implementation to the target |

### 0.1 Core Conclusion

Tabvis already has Secret References, Keychain/Keyring support, browser ownership, a policy engine,
and partial redaction. However, these capabilities still primarily operate inside one Python process
and one operating-system permission domain.

This design requires “model decisions” and “secret use” to reside in two separate trust domains:

1. Agent Runtime can only request use of a credential profile ID.
2. Credential Broker reads secrets and completes authentication in a separate trusted process.
3. Agent Runtime cannot access the Secret Provider, raw cookies, Storage State, or browser debugging
   connection.
4. All data returned to the Agent, model, logs, artifacts, and telemetry passes through DLP Gateway
   first.

### 0.2 Security Claim

“Secrets are not exposed to the intelligent agent” does not mean that a password exists only in the
Secret Provider and one Python object during authentication. To complete web login, the password
must briefly enter:

- The Secret Provider's return buffer.
- Protected memory in Credential Executor.
- Browser input and renderer processes.
- The page at the authorized target Origin.
- The TLS-encrypted request sent to the target website.

This design guarantees that plaintext secrets **MUST NOT** enter model context, Agent-callable tool
parameters, normal browser RPC, task history, Session Transcript, Browser Artifact, ordinary logs,
audit content, exception stacks, or telemetry.

---

## 1. Goals and Non-Goals

### 1.1 Goals

1. The Agent requests authentication only through `credential_profile_id`.
2. Usernames, passwords, TOTP seeds, and TOTP verification codes do not enter model context.
3. Verify HTTPS, top-level Origin, the complete iframe Origin chain, redirect state, and profile
   ownership before authentication.
4. Exclusively lock the browser session during authentication and prohibit normal browser
   interaction and observation.
5. Authorize one authentication attempt through a single-use, short-lived Capability bound to the
   session and Origin.
6. Support single-page login, two-stage login, TOTP, and site-specific adapters.
7. Safely transition to human operation when automatic authentication cannot complete.
8. Protect cookies, tokens, and Storage State at the same level as passwords.
9. Route all outbound model, log, artifact, and telemetry data through unified DLP.
10. Audit secret-use behavior without recording secret content.
11. Leave no reusable Capability or uncleared authentication field after process crashes, timeouts,
    cancellation, or restart.

### 1.2 Non-Goals

- Bypass CAPTCHAs, anti-bot systems, or website terms of service.
- Bypass WebAuthn user verification, hardware security keys, or operating-system user-presence
  requirements.
- Automatically extract passwords from web pages, email, chat, or model output as new credentials.
- Allow the Agent to create, modify, or resolve Secret References.
- Guarantee that a compromised target website cannot read a password submitted to it.
- Support every website in the first phase; unknown websites may fail safely or hand off to a human.

---

## 2. Threat Model

### 2.1 Attackers in Scope

| Attacker | Example | Must defend |
|---|---|---|
| Malicious or prompt-injected web page | Page asks the model to reveal a password, execute JavaScript, or upload cookies | Yes |
| Prompt-injected Agent | Requests Keychain, Secret Ref, cookie, or browser-profile access | Yes |
| Ordinary tool bypass | Bash, Read, BrowserSnapshot, BrowserType, or MCP obtains a secret | Yes |
| Cross-task privilege escalation | Task B reuses Task A's authenticated state | Yes |
| Origin spoofing | Homograph domain, open redirect, malicious iframe, or HTTP downgrade | Yes |
| Log and telemetry leaks | URLs, headers, exception arguments, screenshots, DOM, or traces carry secrets out | Yes |
| Capability replay | Reuse of an already approved authentication capability | Yes |
| Process crash | Core dump, temporary file, uncleared field, or orphaned lock | Yes |
| Ordinary local user | Reads a plaintext secret file or browser profile | Production mode must defend |

### 2.2 Attackers Out of Scope

The following threats require higher-level infrastructure and cannot be guaranteed by this module
alone:

- An attacker with root, kernel, hypervisor, or Broker-process debugging privileges.
- An attacker who has compromised the Secret Provider, operating-system Keychain, or target website.
- A supply-chain attacker who can modify a signed Tabvis release or production configuration.
- An attacker who can read physical memory or mount a hardware side-channel attack.

### 2.3 Deployment Security Levels

| Level | Isolation | Capability claim |
|---|---|---|
| L0 development mode | In-process calls | Functional debugging only; MUST NOT claim security isolation |
| L1 process isolation | Separate process under the same OS user | Prevents accidental leakage, but not arbitrary code running as that user |
| L2 production mode | Separate process with a distinct OS identity or strong sandbox | Meets this document's Agent/credential isolation goal |

Production releases MUST use L2. When Agent Runtime has Bash, file-read, or arbitrary-extension
capabilities, merely splitting it into two processes under the same user does not create a security
boundary.

---

## 3. Current Implementation and Gaps

### 3.1 Existing Capabilities

| Capability | Current module | Status |
|---|---|---|
| Secret Reference | `tabvis/browser/secret_store.py` | **Current** |
| macOS Keychain / system Keyring | `tabvis/browser/secret_store.py` | **Current** |
| Identity stores only Credential Ref | `tabvis/browser/identity.py` | **Current** |
| Explicit Storage State import/export | `tabvis/browser/identity_store.py` | **Current** |
| 1:1 Agent-to-Browser Profile mapping | `tabvis/browser/manager.py` | **Current** |
| Browser-operation policy entry point | `tabvis/browser/policy_guard.py` | **Current** |
| Default redaction of tool-input artifacts | `tabvis/browser/artifacts.py` | **Current** |
| Memory URL and input sanitization | `tabvis/agent/mem/sanitizer.py` | **Current** |

### 3.2 Gaps That Must Be Fixed

1. `identity_store.resolve_credential()` can return plaintext inside the Agent process.
2. `BrowserTypeInput.text` is a model-visible tool parameter and cannot be used for password entry.
3. Browser Runtime can return page snapshots, screenshots, and DOM to the model.
4. Browser Artifact currently stores raw URLs, titles, and DOM.
5. The current Chromium Profile preserves authenticated state across runs without a per-user,
   per-task authentication-session lease.
6. The current Secret Store can degrade to a plaintext `0600` JSON file.
7. Write arguments passed to the macOS `security` CLI briefly appear in process arguments.
8. The current browser lock represents in-process workspace ownership, not a cross-process
   authentication lease.
9. Policy for interaction with already-open pages cannot yet reliably evaluate the live Origin.
10. The Credential Injection test section in
    `tests/services/test_phase6_secrets_observation.py` is still empty.

---

## 4. Target Architecture

See the original design document for the complete trust-boundary and Browser Host requirements.

### 4.1 Trust Boundaries

| Component | May access | Prohibited access |
|---|---|---|
| Model / Agent Runtime | Profile ID, redacted page, authentication result | Secret Ref resolution, plaintext, cookies, Storage State |
| Run Orchestrator | Trusted task/session/user context and authentication state | SecretValue |
| Credential Broker | Profile metadata, policy, Capability | Model context, ordinary tool history |
| Credential Executor | Short-lived ResolvedCredentials and restricted browser control | Agent Transcript, general-purpose logs |
| Browser Host | Page and browser Context | Secret Provider management interface |
| Secret Provider | Secret Ref and secret value | Agent, page content, task prompt |
| DLP Gateway | Outbound data to inspect and Canary fingerprints | Proactive Secret Ref resolution |

---

The data model, internal interfaces, flows, policies, adapters, sessions, DLP, auditing, concurrency,
module layout, phased implementation, test plan, configuration and operations, open decisions, and
final security properties are described in the in-repository implementation and design material.
This copy provides stable anchors for code references; the team-shared design document remains the
authoritative complete specification.

## 15. Phased Implementation (Summary and Implementation Status)

- **Phase 0 ✅**: Security contract and test skeleton. `tabvis/authentication/` (models, errors,
  policy, capabilities, profile_store, audit, secrets), `tabvis/dlp/canary.py`, Agent tool
  `BrowserAuthenticate` (only `credential_profile_id`), production-mode prohibition of plaintext
  backends in `secret_store`, and deprecation of `resolve_credential()`.
- **Phase 1 ✅**: In-process functional prototype (L0). `totp.py` (RFC 6238), `approval.py`,
  `policy_engine.py`, and `adapters/` (restricted AuthenticationBrowser, generic_password, registry).
- **Phase 2 ✅**: Credential Broker process isolation (L1). `tabvis/credential_broker/` (secret
  providers, executor, broker, Unix-socket protocol/server with SO_PEERCRED, and hardening) plus
  `authentication/broker_client.py`.
- **Phase 3 ✅**: Browser Host and cross-process authentication leases. `browser/auth_lease.py`,
  `browser/auth_browser.py`, and `browser/host.py`; `policy_guard` rejects ordinary browser tools
  during authentication (`browser_authentication_locked`). A separate OS identity/sandbox for L2 is
  deployment configuration.
- **Phase 4 ✅**: Session Vault and task isolation. `tabvis/session_vault/` (Envelope Encryption, AAD
  binding of user+task+profile+session, task-level isolation, gated cross-task reuse, and cascading
  deletion).
- **Phase 5 ✅**: End-to-end DLP and external Providers. `tabvis/dlp/` (gateway, URL, text, image),
  fail-closed Canary matches, and 1Password/Vault Providers. Connecting DLP to all existing egress
  paths remains later integration work.
- **Phase 6 🚧**: Managed Authentication Runtime Integration. The first runtime integrations have
  landed (trusted context, the Tool→Broker call path, Playwright Controller, lease
  heartbeat/cancellation, Vault lifecycle, and major DLP egress paths); L2 deployment and complete
  production security acceptance remain in progress. This phase connects the independent Phase 0–5
  components to Gateway, CLI, Run Orchestrator, and the real Playwright Browser Runtime to form an
  end-to-end authentication path that can be enabled, cancelled, resumed, and accepted for
  production security. See §16 for the detailed plan.

> Note: Component-level security logic for Phases 0–5 is implemented and covered by unit tests, but
> it does not yet form a production end-to-end path. L2 strong isolation (separate OS user,
> container, or remote Worker), the real Playwright Browser Host, and connecting DLP Gateway to every
> egress path are unified under Phase 6. Deployment choices still depend on the decisions in §18 of
> the complete specification.

## 16. Phase 6 — Managed Authentication Runtime Integration

### 16.1 Goal and Definition of Done

The purpose of Phase 6 is not to add another authentication core, but to assemble the Phase 0–5
models, Broker, Executor, Browser Host, Session Vault, and DLP components into the default runtime.
After completion:

1. When `TABVIS_AUTHENTICATION_ENABLED=1`, `BrowserAuthenticate` MUST perform real authentication and
   MUST NOT continue returning a hard-coded `internal_authentication_error`.
2. The Agent can still submit only `credential_profile_id`. `task_id`, `user_id`, `agent_id`,
   `browser_session_id`, Origin, and Capability MUST come from the trusted runtime.
3. Development mode MAY use L0 for ease of debugging. L1 uses a separate Broker process. Production
   MUST use L2 and fail closed whenever isolation, auditing, or secure Secret Provider requirements
   cannot be met.
4. Authentication success, failure, timeout, cancellation, process crash, and run completion MUST
   each have a deterministic cleanup path.
5. All data that may leave the trusted domain MUST pass through the unified DLP Gateway.

Managed authentication remains disabled by default and MUST NOT be declared production-ready until
it passes the §16.10 acceptance gate.

### 16.2 Gateway / CLI Broker Lifecycle and Configuration Assembly

Gateway and CLI MUST share one composition root that constructs and manages managed-authentication
dependencies:

- Select L0, L1, or L2 according to `TABVIS_CREDENTIAL_BROKER_MODE`; production configuration MUST
  NOT silently downgrade to a lower level.
- Resolve `TABVIS_CREDENTIAL_BROKER_ENDPOINT` and validate the Unix-socket path, parent-directory
  permissions, ownership, and platform length limit. When not explicitly configured, use a short
  runtime directory with controlled permissions.
- On `--serve`, start or connect to the Broker, perform startup hardening and health checks, stop
  accepting new requests during shutdown, wait for or cancel in-flight authentication, remove the
  socket, and reclaim expired Capabilities and leases.
- The one-shot CLI creates a short-lived Broker only for the current process/run and performs cleanup
  after both normal and abnormal termination.
- Assemble Profile Store, Secret Provider, Approval Service, Audit Sink, Capability Store, Browser
  Host client, Session Vault, and DLP Gateway. Individual entry points MUST NOT create inconsistent
  singletons.
- If configuration is missing, the Secret Provider is unsafe, auditing is unavailable, or the
  Broker is unhealthy, the tool should return a stable error code without resolving any secret.

### 16.3 Trusted Runtime Context Injection by Orchestrator

Run Orchestrator is the only trusted context bridge between Agent requests and internal Broker
requests:

- Agent tool input continues to be validated by `AgentAuthenticationRequest`; the only permitted
  field is `credential_profile_id`, and extra fields MUST be rejected.
- Orchestrator reads `task_id`, `user_id`, `agent_id`, and `browser_session_id` from the current
  Durable Agent, Run, Browser Binding, and authenticated principal, then generates a unique
  `request_id`.
- Fields with the same names in model messages, tool parameters, URL parameters, or client request
  bodies MUST NOT be trusted.
- Before creating an internal `AuthenticationRequest`, verify that the run remains executable, the
  Browser Binding belongs to the current Agent, the user principal has not changed, and the request
  has not been cancelled.
- Cancellation state MUST continue propagating to Broker/Executor; it cannot be sampled only once
  when the request starts.
- Missing, conflicting, or stale trusted context fails closed with a stable error code and does not
  expose internal identifiers or exception details to the Agent.

### 16.4 `BrowserAuthenticateTool → BrokerClient → Broker` Call Path

`BrowserAuthenticateTool.call()` MUST call the authentication service injected by the runtime rather
than reading Secret Store or BrowserService directly:

1. The tool validates `credential_profile_id` and submits an Agent-visible request to Orchestrator.
2. Orchestrator calls `enrich_request()` with the trusted fields from §16.3.
3. Runtime selects `InProcessBrokerClient` or `SocketBrokerClient`, applies an overall timeout, and
   binds the cancellation signal.
4. Broker rereads profile ownership and live browser context, then executes Policy, Approval,
   Capability, and Executor flows.
5. The response MUST be revalidated through the strict `AuthenticationResult` schema, allowing only
   `success`, `authenticated_origin`, `requires_human_interaction`, and `error_code`.
6. On IPC disconnect, timeout, Broker restart, or malformed response, Capability, leases, and
   sensitive fields MUST be cleared; the tool returns only a stable error code.

Broker concurrency control MUST atomically perform “check and acquire” within one critical section.
Two authentication attempts MUST NOT execute concurrently in the same Browser Session. The
`max_uses` check and successful-use counter update require equivalent atomicity or a persistent
transaction.

### 16.5 Real Playwright `PageController`

Browser Host implements a Playwright-backed `PageController` while retaining a restricted interface:

- Browser Host exclusively owns `Browser`, `BrowserContext`, `Page`, CDP endpoint, and profile path.
  These objects or addresses MUST NOT be returned to Agent, Broker, or Executor.
- `current_context()` calculates top-level, frame, and ancestor Origins, `page_id`, and monotonically
  increasing `navigation_generation` from the live Playwright frame tree. It cannot accept an Origin
  asserted by the caller.
- `find_field()` returns only a short-lived opaque handle generated by Browser Host. The handle MUST
  be bound to page/frame/navigation generation and invalidated on navigation or lease completion.
- `type_bytes()` is the only interface through which a secret enters the browser. Before every
  input, revalidate Capability, page, complete frame chain, HTTPS, and certificate status. The
  implementation MUST NOT write secrets into ordinary logs, traces, exception arguments, or
  Agent-visible tool input.
- `clear_fields()` MUST cover username, password, and TOTP fields. If successful cleanup cannot be
  confirmed, destroy the entire BrowserContext rather than returning it to ordinary browser tools.
- During authentication, prohibit screenshots, DOM snapshots, evaluate, downloads, and ordinary
  browser RPC. Apply `post_auth_redaction_spec()` to the first visible capture after authentication.

### 16.6 Authentication-Lease Heartbeats, Cancellation, and Crash Recovery

- After `begin_authentication()` acquires a lease, it MUST automatically start a heartbeat task. The
  heartbeat interval MUST NOT exceed one third of the TTL; callers should not depend on manual
  `lease.heartbeat()` calls.
- Heartbeat renewal, expiration reclamation, and release MUST use cross-process atomic mechanisms and
  verify `lease_id` before updating or deleting. An old holder MUST NOT overwrite a new lease.
- User cancellation, run cancellation, overall timeout, Broker/Executor crash, and Browser Host
  disconnect MUST all trigger the same cleanup state machine: stop input, invalidate Capability,
  clear fields, terminate the heartbeat, release the lease, and destroy the Context whenever state
  is uncertain.
- Browser Host reclaims expired leases at startup. Ordinary Agent RPC continues to fail closed during
  reclamation.
- Long flows such as human approval and push MFA MUST retain the lease within an explicit overall
  timeout and MUST NOT silently unlock because of a fixed 120-second TTL.

### 16.7 Session Vault Creation, Restoration, and Cleanup

- Browser Host may export `storage_state` to Session Vault for encrypted storage only after a strong
  authentication-success signal, a valid final Origin, and confirmed cleanup of sensitive fields.
- Session creation uses the profile's TTL, reuse policy, and allowed Origins. Persisted content may
  contain only an encrypted envelope and MUST NOT fall back to a plaintext file.
- Before restoration, recheck the same user, task binding, profile state, expiration/revocation
  status, and requested Origins. Restoration MUST happen inside Browser Host; raw cookies, tokens,
  and Storage State MUST NOT pass through Agent or ordinary Gateway APIs.
- At run/task completion, delete sessions that cannot be reused across tasks. Cascade deletion when
  a profile is disabled or deleted, the user revokes access, or key rotation fails. Remove expired
  records at startup and during scheduled maintenance.
- On decryption failure, key-ID mismatch, or corrupt records, fail closed and delete the
  unrecoverable record without returning partial state.

### 16.8 DLP Egress Integration

Runtime creates one unified `DLPGateway`. The following egress paths MUST call it before
serialization or persistence:

- Model requests and tool results.
- Session Transcript, run events, and Context Pack.
- Browser Artifact, screenshot metadata, download metadata, and error-page summaries.
- Ordinary logs, audit, telemetry, and crash reports.
- Gateway/legacy HTTP API and SSE responses.
- Temporary files, debug traces, and human-handoff material.

DLP MUST handle headers, URLs, nested mappings/lists, Pydantic models, and all other payload shapes
used in practice. A Canary or prohibited-object match MUST block the complete egress and trigger a
unified response: invalidate Capability, end the authentication lease, mark the associated Session
as non-reusable, and write a secret-free `dlp.secret_blocked` audit event. Failure inside the DLP
hook MUST NOT convert a blocked result into an allow.

### 16.9 Implementation Order

Deliver the following independently acceptable slices:

1. **Composition**: Gateway/CLI lifecycle, configuration validation, Broker health, and secure
   startup/shutdown.
2. **Trusted call path**: Orchestrator context injection and
   `BrowserAuthenticateTool → BrokerClient → Broker`.
3. **Browser isolation**: Real `PageController`, cross-process leases, heartbeats, cancellation, and
   Context destruction.
4. **Session lifecycle**: Session Vault persistence, restoration, run/task cleanup, and cascading
   revocation.
5. **Egress enforcement**: Connect DLP to every egress path and remove existing bypasses.
6. **Production gate**: L2 deployment, cross-platform testing, fault injection, security review, and
   release documentation.

Every slice MUST remain disabled by default or fail closed. Secrets MUST NOT be temporarily exposed
to the Agent merely to enable development of a later slice.

### 16.10 Tests and Production Security Acceptance

Phase 6 requires at least the following automated tests:

- One end-to-end real tool-call test proving that enabling the feature no longer always returns
  `internal_authentication_error`, and that the Agent sees only `AuthenticationResult`.
- Startup, health, shutdown, and crash-recovery tests for both Gateway daemon and one-shot CLI
  composition.
- Race tests for concurrent authentication, same-session reentry, `max_uses`, Capability replay, and
  lease expiration/reclamation.
- Adversarial tests for navigation, iframe switching, HTTP downgrade, certificate errors, malicious
  selectors/handles, and page replacement.
- Fault-injection tests for user cancellation, run cancellation, Broker/Browser Host/Executor crash,
  truncated IPC, and timeout.
- A human-MFA flow longer than the default lease TTL, proving that ordinary Browser RPC remains
  rejected throughout heartbeats.
- Session Vault tests for same-user/cross-task/Origin constraints, expiration, revocation, key
  errors, and prohibition of plaintext fallback.
- DLP Canary tests for the model, Transcript, Artifact, logs, audit, telemetry, API/SSE, and crash
  reports, including mixed header/body payloads, nested objects, and model objects.
- Linux/macOS tests for Unix sockets, peer credentials, permissions, and path length. If production
  supports Windows, add equivalent IPC and ACL tests.
- Installation-matrix tests proving that the base installation or an explicit managed-auth extra
  includes the cryptographic dependencies required by Session Vault.

Production release MUST also satisfy all of the following:

1. L2 isolation passes security review. Agent Runtime cannot read the Secret Provider, Broker memory,
   browser profile, CDP endpoint, raw cookies, or Storage State.
2. With `TABVIS_AUTH_AUDIT_FAIL_CLOSED=1`, an audit-write failure blocks authentication success
   rather than swallowing the exception and continuing.
3. No failure path places test secrets in model context, tool parameters, logs, exceptions,
   artifacts, transcripts, or APIs.
4. The full test suite, static checks, and cross-platform IPC tests pass without skipping security
   tests to obtain a green result.
5. Operations documentation covers configuration, startup, key rotation, revocation, auditing,
   recovery, and downgrade policy.

### 16.11 Current Implementation Progress

Implemented:

- `authentication/runtime.py` is the shared composition root for CLI and Gateway. Development mode
  assembles an L0 `CredentialBroker`; `ipc`/`production` use a Unix socket validated for path, type,
  ownership, and `0600` permissions. Production mode fails closed at startup without explicit L2
  verification or a secure Secret Backend.
- `ToolUseContext` receives principal/run/session/browser binding from Orchestrator.
  `BrowserAuthenticateTool` constructs only `AgentAuthenticationRequest`, executes through Runtime,
  BrokerClient, and Broker, and revalidates a strict `AuthenticationResult`.
- Browser Host integrates the real `PlaywrightPageController`, BrowserService action exclusion,
  cross-process lease locks, automatic heartbeats, Capability invalidation on cancellation/timeout,
  field cleanup, Context destruction when necessary, and masking for the first post-authentication
  screenshot.
- Broker acquisition is now an atomic critical section. Session Vault is integrated with creation on
  success, restoration based on a strong cookie signal, task-completion cleanup, cascading
  revocation when profiles are disabled/deleted, and startup expiration cleanup.
- Model tool results, Transcript, Context Pack, run previews, Browser Artifact, HTTP/SSE, and
  authentication audit now use the unified DLP Gateway. A Canary match marks an in-flight
  authentication Context for destruction, clears Capabilities, and revokes Vault sessions for the
  active profile.
- Automated tests cover Tool→Runtime→Broker→Browser/Vault, missing trusted context, cancellation,
  lease heartbeats, Broker concurrency, fail-closed auditing, mixed-object DLP, and a real Chromium
  PageController.

Not yet accepted as “Phase 6 complete”:

- Starting, stopping, and health protocols for an L2 Broker under a separate OS user, container, or
  remote Worker remain deployment work. The current production gate rejects startup unless L2 is
  explicitly confirmed.
- The Linux/macOS IPC matrix, Broker/Browser Host/Executor crash fault injection, real long-running
  MFA flows, audits of all telemetry/crash-report/temporary-trace egress, formal security review, and
  operations runbook remain to be completed.
