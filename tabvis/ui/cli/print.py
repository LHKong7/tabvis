"""Headless ``-p/--print`` runner

Skeleton scope: consume the SDKMessage stream from :func:`tabvis.agent.query_engine.ask` and write it to
stdout in one of three output formats:

* ``text`` (default): print the final ``result`` text only;
* ``json``: print the terminal ``result`` SDKMessage as JSON;
* ``stream-json``: print every SDKMessage as NDJSON.

The session id is registered in bootstrap state (``switch_session``) so the transcript persists to
``<sessionId>.jsonl`` (recorded inside :func:`tabvis.agent.query_engine.ask`) and the batched write queue is
flushed before the process exits (no graceful-shutdown hook runs on the headless path).

Slash commands: a prompt that is a leading ``/<known-command>`` is routed through the faithful
``process_slash_command`` processor (:func:`_maybe_process_slash_command`). Local commands
(``/dynamic-workflow``, saved ``/<name>`` workflows) run immediately and return text with no model
turn; prompt/skill commands expand into the turn. Anything else is treated as a plain prompt.

Not supported in this build: resume/continue, the stream-json *input* mode, SDK permission-prompt
tool, fork/rewind, MCP connect, idle timeout, skill hot-reload.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

from tabvis.bootstrap.state import switch_session
from tabvis.constants.prompts import get_system_prompt
from tabvis.agent.query.deps import QueryDeps
from tabvis.agent.query_engine import ask
from tabvis.browser.manager import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROFILE,
    bind_agent,
    detach_agent,
    init_browser_session,
    start_browser_warmup,
    unbind_agent,
)
from tabvis.state.app_state_store import create_app_state_store
from tabvis.tool import (
    ToolUseContext,
    ToolUseContextOptions,
    get_empty_tool_permission_context,
)
from tabvis.agent.tools import assemble_tool_pool, get_tools
from tabvis.types.ids import as_session_id
from tabvis.utils.abort import AbortController
from tabvis.utils.cwd import get_cwd
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.messages import create_user_message
from tabvis.utils.model.model import get_main_loop_model


def _write_ndjson(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, default=str) + "\n")
    sys.stdout.flush()


def _build_tool_use_context(
    *,
    tools: Any,
    model: str,
    mcp_clients: list[Any],
    mcp_resources: dict[str, Any],
    agent_definitions: Any,
    commands: list[Any],
    store: Any,
) -> ToolUseContext:
    """Build the headless ``ToolUseContext`` used for slash-command processing and the model turn.

    Carries the resolved ``commands`` set so the slash processor can resolve ``/<name>`` and so a
    workflow command's ``call`` receives a fully-wired context (tools / agent_definitions / abort /
    app state) — exactly what ``run_workflow`` / ``generate_workflow_script`` need.
    """
    options = ToolUseContextOptions(
        tools=tools,
        main_loop_model=model,
        is_non_interactive_session=True,
        query_source="sdk",
        mcp_clients=mcp_clients,
        mcp_resources=mcp_resources,
        agent_definitions=agent_definitions,
        commands=commands,
    )
    return ToolUseContext(
        options=options,
        abort_controller=AbortController(),
        get_app_state=store.get_state,
        set_app_state=store.set_state,
        messages=[],
        set_in_progress_tool_use_ids=lambda _f: None,
    )


async def _maybe_process_slash_command(
    prompt: str, context: ToolUseContext
) -> dict[str, Any] | None:
    """If ``prompt`` is a leading ``/<known-command>``, resolve and route it for the headless turn.

    Returns ``{"seed_messages", "should_query", "result_text"}`` or ``None`` (not a slash command,
    or names no known command — then treated as a plain prompt rather than erroring, e.g. a message
    that merely starts with ``/``).

    * **local** commands (``/dynamic-workflow``, saved ``/<name>`` workflows) run now via the
      faithful ``process_slash_command`` (which calls ``load().call()``) — they come back with
      ``should_query=False`` and a ``result_text``, so no model turn follows.
    * **prompt** / skill commands are expanded into a single user message via
      ``get_prompt_for_command`` (the same expansion the ``Skill`` tool performs) and queried. The
      full ``process_slash_command`` message structure for prompt commands targets the interactive
      query pipeline (command-loading metadata + a ``command_permissions`` attachment that the API
      message conversion rejects), so the headless skeleton feeds the expanded content directly.
    """
    stripped = prompt.lstrip()
    if not stripped.startswith("/"):
        return None
    from tabvis.ui.commands import find_command
    from tabvis.utils.slash_command_parsing import parse_slash_command

    parsed = parse_slash_command(stripped)
    if not parsed:
        return None
    command = find_command(parsed.command_name, context.options.commands)
    if command is None:
        return None

    if command.type == "prompt" and command.get_prompt_for_command is not None:
        blocks = await command.get_prompt_for_command(parsed.args, context)
        return {
            "seed_messages": [create_user_message(content=blocks)],
            "should_query": True,
            "result_text": None,
        }

    from tabvis.utils.process_user_input.process_slash_command import process_slash_command

    result = await process_slash_command(stripped, [], [], [], context)
    return {
        "seed_messages": result["messages"],
        "should_query": result.get("shouldQuery", False),
        "result_text": result.get("resultText"),
    }


def _skill_listing_reminder(commands: list[Any]) -> str | None:
    """A ``<system-reminder>`` listing the model-invocable skills, or ``None`` if there are none.

    Headless skill discovery: the interactive attachment pipeline injects a "here are your skills"
    reminder, but :func:`tabvis.agent.query_engine.ask` bypasses that pipeline — so in ``-p`` the model only
    sees skills *passively* in the Skill tool's description and rarely engages them. We surface the
    same listing proactively in the conversation instead.
    """
    from tabvis.agent.tools.skill_tool import format_available_skills

    skills = [
        c
        for c in commands
        if getattr(c, "type", None) == "prompt"
        and getattr(c, "user_invocable", True) is not False
        and not getattr(c, "disable_model_invocation", False)
    ]
    listing = format_available_skills(skills)
    if not listing.strip():
        return None
    return (
        "<system-reminder>\n"
        "You have skills available that package specialized methodology and domain knowledge. When "
        "the task matches a skill, invoke it with the Skill tool and follow its guidance BEFORE "
        "doing the work yourself.\n\n"
        f"Available skills:\n{listing}\n"
        "</system-reminder>"
    )


async def run_headless(
    prompt: str,
    *,
    model: str | None = None,
    output_format: str = "text",
    can_use_tool: Any | None = None,
    tools: Any | None = None,
    deps: QueryDeps | None = None,
    max_turns: int | None = None,
    resume_target: Any | None = None,
) -> dict[str, Any] | None:
    """Run a single headless turn and write the result to stdout. Returns the result message.

    Thin consumer of :func:`stream_agent` — the session machinery (browser warm-up, session
    record, cleanup drain) lives there so the SSE server (``tabvis.browser.server``) can reuse
    the exact same path.

    ``resume_target`` (a ``tabvis.agent.resume_plus.ResumeTarget``) continues an existing session:
    its resolved ``session_id`` / ``project_dir`` / ``run_id`` are threaded into ``stream_agent`` and
    the prior conversation is replayed. A one-shot CLI is never resident, so the resolver already
    reports a cold ``relaunched_profile`` recovery — this path never claims a live browser.
    """
    rt = resume_target
    if rt is not None:
        # Announce, on stderr, what was actually restored (§5.2) — separate from the stdout result.
        from tabvis.agent.resume_plus import ResumeResult

        result = ResumeResult(
            resume_mode=rt.mode, agent_id=rt.agent_id, session_id=rt.session_id,
            run_id=rt.run_id, browser_recovery=rt.browser_recovery,
            warnings=list(rt.warnings),
        )
        print(
            f"tabvis: resuming session {rt.session_id} (mode={rt.mode}, "
            f"browser={rt.browser_recovery}, run={rt.run_id}).",
            file=sys.stderr,
        )
        for w in result.warnings:
            print(f"tabvis: warning: {w}", file=sys.stderr)

    last_result: dict[str, Any] | None = None
    async for message in stream_agent(
        prompt,
        model=model,
        can_use_tool=can_use_tool,
        tools=tools,
        deps=deps,
        max_turns=max_turns,
        include_partial_messages=(output_format == "stream-json"),
        teardown=True,
        session_id=(rt.session_id if rt is not None else None),
        resume=(rt is not None and rt.mode != "fresh"),
        profile=(rt.profile if rt is not None else DEFAULT_PROFILE),
        project_dir=(rt.project_dir if rt is not None else None),
        run_id=(rt.run_id if rt is not None else None),
        resume_mode=(rt.mode if rt is not None else "fresh"),
        principal_id=(rt.principal_id if rt is not None else "principal_local"),
    ):
        if output_format == "stream-json":
            _write_ndjson(message)
        if message.get("type") == "result":
            last_result = message

    if output_format == "text":
        if last_result is not None:
            print(last_result.get("result", ""))
    elif output_format == "json":
        print(json.dumps(last_result, default=str))

    return last_result


async def stream_agent(
    prompt: str,
    *,
    model: str | None = None,
    can_use_tool: Any | None = None,
    tools: Any | None = None,
    deps: QueryDeps | None = None,
    max_turns: int | None = None,
    include_partial_messages: bool = False,
    teardown: bool = True,
    agent_id: str = DEFAULT_AGENT_ID,
    profile: str | None = DEFAULT_PROFILE,
    session_id: str | None = None,
    resume: bool = False,
    extra_system_context: str | None = None,
    owns_system_context: bool = False,
    project_dir: str | None = None,
    run_id: str | None = None,
    resume_mode: str = "fresh",
    principal_id: str = "principal_local",
) -> Any:
    """Run one agent session, yielding each SDKMessage as it is produced.

    This is the single source of truth for a headless agent run: session identity, the eager
    browser warm-up, tool/MCP assembly, slash routing, the model loop, and teardown. Both the CLI
    (:func:`run_headless`) and the SSE server consume it.

    ``agent_id`` names this run and **bundles** a browser to the agent — reserved at spawn and held
    for the agent's whole life (see services/browser/manager). It is bound to a ContextVar so every
    tool call inside this task targets the right one. ``profile`` picks that agent's Chromium profile
    dir: ``"default"`` is the shared logged-in browser (held exclusively by its owner until quit),
    ``None`` gives the agent an isolated per-agent profile so it can run in parallel with others.

    ``resume=True`` replays the ``session_id``'s prior turns into the model before the new prompt, so
    re-running an agent continues its conversation instead of starting blank (the server sets this
    when it reuses an existing agent). It is a no-op for a fresh session (nothing to replay).

    ``teardown=True`` (the CLI, one-shot) drains the whole cleanup registry when the run ends,
    closing the browser with it. ``teardown=False`` (a long-lived server) keeps the bundled browser
    open past the run — it is the agent's environment until the user quits the agent — releasing
    only the *actively-driving* claim; the server closes everything once, at process shutdown.
    """
    from tabvis.agent.main import default_can_use_tool  # local import avoids a cycle

    token = bind_agent(agent_id)
    model = model or get_main_loop_model()
    permission_context = get_empty_tool_permission_context()
    store = create_app_state_store()
    if can_use_tool is None:
        can_use_tool = default_can_use_tool
    # Callers that need to know the session id up-front (the server, so it can put it on the agent
    # record before the run starts) pass it in; everyone else gets a fresh one.
    session_id = session_id or str(uuid.uuid4())
    # Register the session id in bootstrap state so the persistence layer agrees with the id we
    # advertise to the SDK stream. record_transcript / get_transcript_path both derive the on-disk
    # file name and the stamped "sessionId" from get_session_id() — NOT from this local variable —
    # so without switch_session() the transcript would land under the bootstrap-default uuid and
    # resume-by-id could not find it. On a Resume Plus the resolver supplies ``project_dir`` (the
    # ORIGINAL session's project directory, possibly under a different cwd), so the transcript is read
    # and written where it actually lives rather than re-derived from the current cwd.
    switch_session(as_session_id(session_id), project_dir=project_dir)

    # Bind the immutable per-Run locator (Resume Plus §4.1) for this task. Additive: writers that
    # still read process-global session state are unaffected; this is the seam they migrate onto.
    from tabvis.agent.run_context import RunContext, set_run_context

    set_run_context(
        RunContext(
            principal_id=principal_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id or "",
            cwd=get_cwd(),
            project_dir=project_dir,
            resume_mode=resume_mode,
        )
    )

    # --- Bundle the browser to the agent, at spawn ----------------------------------------
    # init_browser_session RESERVES the workspace for this agent now (it owns it for its whole life),
    # and start_browser_warmup LAUNCHES the persistent Chromium as a background task so it boots in
    # parallel with MCP tool building, slash processing, the system prompt, and the first model call.
    # By the time the model reaches for a Browser* tool the browser is already up and warm.
    # `get_or_create_browser_service` memoizes the in-flight launch, so a tool call awaits this very
    # task — there is never a double launch. TABVIS_BROWSER_EAGER=0 opts out of the eager launch (the
    # reservation still holds; the browser then launches lazily on the first browser tool call).
    init_browser_session(
        session_id=session_id,
        model=model,
        cwd=get_cwd(),
        agent_id=agent_id,
        profile=profile,
    )
    warmup_task = start_browser_warmup()

    # NOTE: `mcp_clients` / `mcp_resources` are bound BEFORE the try so the finally can always
    # reach them, and the try now opens here (not just before the model call) so that a failure
    # in tool building / slash routing still runs the teardown below — previously such a failure
    # skipped the finally entirely, orphaning the browser AND leaking MCP clients.
    mcp_clients: list[Any] = []
    mcp_resources: dict[str, Any] = {}
    try:
        if tools is None:
            tools, mcp_clients, mcp_resources = await _build_tools_with_mcp(permission_context)

        from tabvis.agent.tools.agent_defs import get_agent_definitions_with_overrides

        agent_definitions = get_agent_definitions_with_overrides()

        # Slash-command routing. If the prompt is a leading /<known-command>, run it through the
        # faithful slash processor: local commands (/dynamic-workflow, saved /<name> workflows)
        # execute now and return text (no model turn); prompt/skill commands expand into the turn.
        # Built lazily — a plain prompt pays nothing and `ask` builds its own default context.
        from tabvis.ui.commands import get_commands

        context = _build_tool_use_context(
            tools=tools,
            model=model,
            mcp_clients=mcp_clients,
            mcp_resources=mcp_resources,
            agent_definitions=agent_definitions,
            commands=get_commands(get_cwd()),
            store=store,
        )
        slash_result = await _maybe_process_slash_command(prompt, context)
        seed_messages = slash_result["seed_messages"] if slash_result else None
        should_query = slash_result["should_query"] if slash_result else True
        result_text = slash_result["result_text"] if slash_result else None

        # For a plain prompt (not a slash command), prepend the available-skills listing as a
        # leading content block of the user message — one message, no extra turn — so the headless
        # model is proactively nudged to invoke the relevant skill (see _skill_listing_reminder).
        if slash_result is None:
            reminder = _skill_listing_reminder(context.options.commands)
            if reminder is not None:
                seed_messages = [
                    create_user_message(
                        content=[
                            {"type": "text", "text": reminder},
                            {"type": "text", "text": prompt},
                        ]
                    )
                ]

        # Session RESUME: when re-running an agent on an existing session, replay its prior turns so
        # the model actually sees the conversation. Only for a model turn (a local slash command has
        # no model call to seed). The prior turns come first; the new turn is whatever the seed logic
        # above produced (a slash/skill expansion) or the plain prompt — `ask` won't add the prompt
        # itself once `seed_messages` is set, so we include it. Prior envelopes keep their uuids, so
        # record_transcript dedups them and nothing is duplicated on disk.
        if resume and should_query:
            from tabvis.utils.session_storage import load_conversation_for_resume

            prior = await load_conversation_for_resume(session_id)
            if prior:
                new_turn = (
                    seed_messages
                    if seed_messages is not None
                    else [create_user_message(content=prompt)]
                )
                seed_messages = [*prior, *new_turn]

        # A caller (the gateway's Context Runtime) may own project-context assembly: it supplies a
        # pre-assembled block (``extra_system_context``, deterministic and observable via
        # context.pack.built) that already includes project instructions + memory, so the base prompt
        # suppresses its own copies to avoid duplication (``owns_system_context``).
        system_prompt = await get_system_prompt(
            tools, model, None, mcp_clients,
            include_project_instructions=not owns_system_context,
            include_memory=not owns_system_context,
        )
        if extra_system_context:
            system_prompt = [*system_prompt, extra_system_context]

        async for message in ask(
            prompt=prompt,
            tools=tools,
            app_state_store=store,
            can_use_tool=can_use_tool,
            session_id=session_id,
            model=model,
            system_prompt=system_prompt,
            deps=deps,
            max_turns=max_turns,
            mcp_clients=mcp_clients,
            mcp_resources=mcp_resources,
            agent_definitions=agent_definitions,
            include_partial_messages=include_partial_messages,
            context=context,
            seed_messages=seed_messages,
            should_query=should_query,
            result_text=result_text,
        ):
            yield message
    finally:
        # Flush the batched transcript write queue to disk before the loop/process exits. The
        # headless path has no graceful-shutdown hook, so the 100ms-batched appends queued by
        # record_transcript would otherwise be dropped when asyncio.run() closes the loop.
        try:
            from tabvis.utils.session_storage import flush_session_storage

            await flush_session_storage()
        except Exception:  # noqa: BLE001 - flushing is best-effort
            pass

        # Settle the browser warm-up BEFORE draining cleanups. The browser only registers its
        # teardown after a *successful* launch, so draining while the launch is still in flight
        # would find nothing to close and leave an orphaned Chromium behind.
        if warmup_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(warmup_task), timeout=10.0)
            except Exception:  # noqa: BLE001 - a failed/slow warm-up must not fail the run
                pass

        if teardown:
            # Drain the cleanup registry (browser teardown + every other registered subsystem).
            # This is the ONLY place it can happen on the headless path: setup_graceful_shutdown()
            # is wired from entrypoints/init.py, which `-p` never calls, and its SIGINT handler
            # deliberately no-ops when -p/--print is in argv. Without this, nothing runs these.
            try:
                from tabvis.utils.cleanup_registry import run_cleanup_functions

                await asyncio.wait_for(run_cleanup_functions(), timeout=10.0)
            except Exception:  # noqa: BLE001 - best-effort; gather() does not swallow
                pass
        else:
            # Server path: the run is over, but the BUNDLED BROWSER STAYS OPEN and still owned by
            # this agent. It is the agent's environment, not a per-run resource — the window keeps
            # its tabs and its logins for the agent's next run. detach_agent only drops the
            # *actively-driving* claim; the bundle is not idle-reaped and lives until the user quits
            # the agent (POST /agents/<id>/quit) or the process exits (cleanup registry).
            try:
                await asyncio.wait_for(detach_agent(agent_id), timeout=10.0)
            except Exception:  # noqa: BLE001 - best-effort
                pass

        if mcp_clients:
            from tabvis.agent.mcp.client import cleanup_all

            await cleanup_all(mcp_clients)

        unbind_agent(token)


async def _build_tools_with_mcp(permission_context: Any) -> tuple[Any, list[Any], dict[str, Any]]:
    """Built-in tools, plus tools/resources from any configured MCP servers.

    Skipped in TABVIS_SIMPLE/bare mode (matching ``main.ts`` ``strictMcpConfig || isBareMode``) and
    when there are no configs. A connection failure falls back to built-in tools (per-server
    isolation lives in ``get_mcp_tools_commands_and_resources``).
    """
    if is_env_truthy(os.environ.get("TABVIS_SIMPLE")):
        return get_tools(permission_context), [], {}
    try:
        from tabvis.agent.mcp.client import get_mcp_tools_commands_and_resources
        from tabvis.agent.mcp.config import get_tabvis_mcp_configs

        configs = get_tabvis_mcp_configs()
        if not configs:
            return get_tools(permission_context), [], {}
        result = await get_mcp_tools_commands_and_resources(configs)
        pool = assemble_tool_pool(permission_context, result["tools"])
        return pool, result["clients"], result["resources"]
    except Exception as error:  # noqa: BLE001 - never let MCP wiring break the headless run
        from tabvis.utils.debug import log_for_debugging

        log_for_debugging(f"[MCP] connection setup failed; continuing without MCP: {error}")
        return get_tools(permission_context), [], {}
