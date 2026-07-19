"""AskUserQuestion tool.

Asks the user one or more multiple-choice questions (the REPL renders an interactive picker).
The tool is ``requiresUserInteraction() === true``: the question UI is driven entirely by the
permission layer (``checkPermissions`` returns ``behavior: 'ask'``), and the collected answers are
fed back in via the permission decision's ``updatedInput`` (so by the time :meth:`call` runs the
``answers`` field is populated). The tool body itself never blocks — it just echoes back
``{questions, answers, annotations?}``.

Headless / non-interactive behavior: the headless permission gate resolves an ``ask`` decision to
**deny** (``docs/SPINE_CONTRACTS.md`` decision 3), so the tool's ``call`` is never reached with
collected answers and the model receives the declined/denied result from the permission layer —
it does NOT block waiting for a prompt. The non-interactive path of ``call`` (no ``answers``
supplied) returns ``answers={}``.

Casing: Python identifiers are snake_case; the validated input / output dicts and the tool_result
block keep their wire keys (``questions``/``options``/``multiSelect``/``answers``/``annotations``;
``tool_use_id``/``type``/``content``). ``multiSelect`` is a real wire key, so the pydantic field
carries ``alias="multiSelect"`` with ``populate_by_name=True``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tabvis.tool import Tool, ToolResult, ToolUseContext, ValidationResult

# ---------------------------------------------------------------------------
# Tool prompt constants
# ---------------------------------------------------------------------------

# Inlined to avoid pulling in the ExitPlanMode tool module just for its name constant.
EXIT_PLAN_MODE_TOOL_NAME = "ExitPlanMode"

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"

ASK_USER_QUESTION_TOOL_TABVIS_WIDTH = 12

DESCRIPTION = (
    "Asks the user multiple choice questions to gather information, clarify ambiguity, "
    "understand preferences, make decisions or offer them choices."
)

# previewFormat -> guidance appended to the base prompt. Keyed by the value returned from
# getQuestionPreviewFormat(); only consulted when a preview format is configured.
PREVIEW_FEATURE_PROMPT: dict[str, str] = {
    "markdown": """
Preview feature:
Use the optional `preview` field on options when presenting concrete artifacts that users need to visually compare:
- ASCII mockups of UI layouts or components
- Code snippets showing different implementations
- Diagram variations
- Configuration examples

Preview content is rendered as markdown in a monospace box. Multi-line text with newlines is supported. When any option has a preview, the UI switches to a side-by-side layout with a vertical option list on the left and preview on the right. Do not use previews for simple preference questions where labels and descriptions suffice. Note: previews are only supported for single-select questions (not multiSelect).
""",  # noqa: E501
    "html": """
Preview feature:
Use the optional `preview` field on options when presenting concrete artifacts that users need to visually compare:
- HTML mockups of UI layouts or components
- Formatted code snippets showing different implementations
- Visual comparisons or diagrams

Preview content must be a self-contained HTML fragment (no <html>/<body> wrapper, no <script> or <style> tags — use inline style attributes instead). Do not use previews for simple preference questions where labels and descriptions suffice. Note: previews are only supported for single-select questions (not multiSelect).
""",  # noqa: E501
}

ASK_USER_QUESTION_TOOL_PROMPT = (
    """Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multiSelect: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label

Plan mode note: In plan mode, use this tool to clarify requirements or choose between approaches """  # noqa: E501
    f"""BEFORE finalizing your plan. Do NOT use this tool to ask "Is my plan ready?" or "Should I proceed?" - use {EXIT_PLAN_MODE_TOOL_NAME} for plan approval. IMPORTANT: Do not reference "the plan" in your questions (e.g., "Do you have feedback about the plan?", "Does the plan look good?") because the user cannot see the plan in the UI until you call {EXIT_PLAN_MODE_TOOL_NAME}. If you need plan approval, use {EXIT_PLAN_MODE_TOOL_NAME} instead.
"""  # noqa: E501
)


# ---------------------------------------------------------------------------
# Question preview format.
#
# Would be set when an SDK consumer opts into a preview format. Headless runs never configure a
# preview format, so this returns None: the base prompt is used and HTML preview validation is
# skipped.
# ---------------------------------------------------------------------------


def get_question_preview_format() -> str | None:
    return None


# ---------------------------------------------------------------------------
# Input schema (validated request shape)
# ---------------------------------------------------------------------------


class QuestionOption(BaseModel):
    """One selectable choice for a question."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        description=(
            "The display text for this option that the user will see and select. Should be "
            "concise (1-5 words) and clearly describe the choice."
        ),
    )
    description: str = Field(
        description=(
            "Explanation of what this option means or what will happen if chosen. Useful for "
            "providing context about trade-offs or implications."
        ),
    )
    preview: str | None = Field(
        default=None,
        description=(
            "Optional preview content rendered when this option is focused. Use for mockups, "
            "code snippets, or visual comparisons that help users compare options. See the tool "
            "description for the expected content format."
        ),
    )


class Question(BaseModel):
    """One question with 2-4 mutually exclusive options."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    question: str = Field(
        description=(
            "The complete question to ask the user. Should be clear, specific, and end with a "
            'question mark. Example: "Which library should we use for date formatting?" If '
            'multiSelect is true, phrase it accordingly, e.g. "Which features do you want to '
            'enable?"'
        ),
    )
    header: str = Field(
        description=(
            f"Very short label displayed as a tabvis/tag (max {ASK_USER_QUESTION_TOOL_TABVIS_WIDTH} "
            'chars). Examples: "Auth method", "Library", "Approach".'
        ),
    )
    options: list[QuestionOption] = Field(
        min_length=2,
        max_length=4,
        description=(
            "The available choices for this question. Must have 2-4 options. Each option should "
            "be a distinct, mutually exclusive choice (unless multiSelect is enabled). There "
            "should be no 'Other' option, that will be provided automatically."
        ),
    )
    multi_select: bool = Field(
        default=False,
        alias="multiSelect",
        description=(
            "Set to true to allow the user to select multiple options instead of just one. Use "
            "when choices are not mutually exclusive."
        ),
    )


class QuestionAnnotation(BaseModel):
    """Per-question annotation from the user."""

    model_config = ConfigDict(extra="forbid")

    preview: str | None = Field(
        default=None,
        description="The preview content of the selected option, if the question used previews.",
    )
    notes: str | None = Field(
        default=None,
        description="Free-text notes the user added to their selection.",
    )


class QuestionMetadata(BaseModel):
    """Optional analytics metadata."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(
        default=None,
        description=(
            'Optional identifier for the source of this question (e.g., "remember" for /remember '
            "command). Used for analytics tracking."
        ),
    )


class AskUserQuestionInput(BaseModel):
    """Validated input for :data:`ask_user_question_tool`.

    Question texts must be unique, and option labels must be unique within each question; this is
    enforced by a ``model_validator(mode="after")``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    questions: list[Question] = Field(
        min_length=1,
        max_length=4,
        description="Questions to ask the user (1-4 questions)",
    )
    answers: dict[str, str] | None = Field(
        default=None,
        description="User answers collected by the permission component",
    )
    annotations: dict[str, QuestionAnnotation] | None = Field(
        default=None,
        description=(
            "Optional per-question annotations from the user (e.g., notes on preview "
            "selections). Keyed by question text."
        ),
    )
    metadata: QuestionMetadata | None = Field(
        default=None,
        description=(
            "Optional metadata for tracking and analytics purposes. Not displayed to user."
        ),
    )

    @model_validator(mode="after")
    def _check_uniqueness(self) -> AskUserQuestionInput:
        # Question texts must be unique, and option labels must be unique within each question.
        question_texts = [q.question for q in self.questions]
        if len(question_texts) != len(set(question_texts)):
            raise ValueError(
                "Question texts must be unique, option labels must be unique within each question"
            )
        for question in self.questions:
            labels = [opt.label for opt in question.options]
            if len(labels) != len(set(labels)):
                raise ValueError(
                    "Question texts must be unique, option labels must be unique within each "
                    "question"
                )
        return self


# ---------------------------------------------------------------------------
# HTML preview validation
# ---------------------------------------------------------------------------

_HTML_DOCUMENT_RE = re.compile(r"<\s*(html|body|!doctype)\b", re.IGNORECASE)
_HTML_EXECUTABLE_RE = re.compile(r"<\s*(script|style)\b", re.IGNORECASE)
_HTML_FRAGMENT_RE = re.compile(r"<[a-z][^>]*>", re.IGNORECASE)


def validate_html_preview(preview: str | None) -> str | None:
    """Lightweight HTML-fragment intent check for the ``preview`` field.

    Not a parser. Returns an error string when the preview looks like a full document, contains
    executable tags, or contains no HTML at all; otherwise ``None``.
    """
    if preview is None:
        return None
    if _HTML_DOCUMENT_RE.search(preview):
        return (
            "preview must be an HTML fragment, not a full document "
            "(no <html>, <body>, or <!DOCTYPE>)"
        )
    if _HTML_EXECUTABLE_RE.search(preview):
        return (
            "preview must not contain <script> or <style> tags. Use inline styles via the style "
            "attribute if needed."
        )
    if not _HTML_FRAGMENT_RE.search(preview):
        return (
            'preview must contain HTML (previewFormat is set to "html"). Wrap content in a tag '
            "like <div> or <pre>."
        )
    return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class AskUserQuestionTool(Tool):
    """``AskUserQuestion`` — ask the user multiple-choice questions."""

    name = ASK_USER_QUESTION_TOOL_NAME
    search_hint = "prompt the user with a multiple-choice question"
    input_schema = AskUserQuestionInput
    output_schema = None  # output schema deferred (not needed for the headless build)
    max_result_size_chars = 100_000
    should_defer = True

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return DESCRIPTION

    async def prompt(self, options: dict[str, Any]) -> str:
        format = get_question_preview_format()
        if format is None:
            # SDK consumer that hasn't opted into a preview format — omit preview guidance
            # (they may not render the field at all).
            return ASK_USER_QUESTION_TOOL_PROMPT
        return ASK_USER_QUESTION_TOOL_PROMPT + PREVIEW_FEATURE_PROMPT[format]

    def user_facing_name(self, input: Any | None = None) -> str:
        return ""

    def is_enabled(self) -> bool:
        return True

    def is_concurrency_safe(self, input: Any) -> bool:
        return True

    def is_read_only(self, input: Any) -> bool:
        return True

    def requires_user_interaction(self) -> bool:
        return True

    async def validate_input(self, input: Any, context: ToolUseContext) -> ValidationResult:
        # HTML preview validation only applies when the consumer opted into the 'html' format.
        if get_question_preview_format() != "html":
            return ValidationResult(result=True)

        questions = _get_questions(input)
        for q in questions:
            for opt in _get_options(q):
                err = validate_html_preview(_get_attr(opt, "preview"))
                if err:
                    label = _get_attr(opt, "label")
                    question_text = _get_attr(q, "question")
                    return ValidationResult(
                        result=False,
                        message=f'Option "{label}" in question "{question_text}": {err}',
                        error_code=1,
                    )
        return ValidationResult(result=True)

    async def check_permissions(self, input: Any, context: ToolUseContext):
        # Always 'ask' — the question UI IS the permission prompt. In headless / non-interactive
        # mode the gate resolves 'ask' to deny (docs/SPINE_CONTRACTS.md decision 3), so the model
        # receives a declined result rather than blocking on a prompt.
        return {
            "behavior": "ask",
            "message": "Answer questions?",
            "updatedInput": input,
        }

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool,
        parent_message,
        on_progress=None,
    ) -> ToolResult[dict[str, Any]]:
        # The answers/annotations are populated by the permission component before call() runs.
        # In the non-interactive path (no answers supplied) this echoes answers={} — call() itself
        # never blocks.
        questions = _serialize_questions(_get_questions(args))
        answers = _get_attr(args, "answers") or {}
        annotations = _get_attr(args, "annotations")

        data: dict[str, Any] = {"questions": questions, "answers": answers}
        if annotations:
            data["annotations"] = _serialize_annotations(annotations)
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        answers: dict[str, str] = data.get("answers") or {}
        annotations: dict[str, Any] = data.get("annotations") or {}

        answer_parts: list[str] = []
        for question_text, answer in answers.items():
            annotation = annotations.get(question_text)
            parts = [f'"{question_text}"="{answer}"']
            if annotation:
                preview = _get_attr(annotation, "preview")
                notes = _get_attr(annotation, "notes")
                if preview:
                    parts.append(f"selected preview:\n{preview}")
                if notes:
                    parts.append(f"user notes: {notes}")
            answer_parts.append(" ".join(parts))
        answers_text = ", ".join(answer_parts)

        return {
            "type": "tool_result",
            "content": (
                f"User has answered your questions: {answers_text}. You can now continue with "
                "the user's answers in mind."
            ),
            "tool_use_id": tool_use_id,
        }


# ---------------------------------------------------------------------------
# small shape helpers (accept either a pydantic model or a plain dict)
# ---------------------------------------------------------------------------


def _get_attr(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _get_questions(input: Any) -> list[Any]:
    questions = _get_attr(input, "questions")
    return list(questions) if questions else []


def _get_options(question: Any) -> list[Any]:
    options = _get_attr(question, "options")
    return list(options) if options else []


def _serialize_questions(questions: list[Any]) -> list[dict[str, Any]]:
    """Round-trip questions back to wire dicts (keeping the ``multiSelect`` wire key)."""
    out: list[dict[str, Any]] = []
    for q in questions:
        if isinstance(q, Question):
            out.append(q.model_dump(by_alias=True, exclude_none=True))
        elif isinstance(q, dict):
            out.append(q)
        else:
            out.append(dict(q))
    return out


def _serialize_annotations(annotations: Any) -> dict[str, Any]:
    if isinstance(annotations, dict):
        result: dict[str, Any] = {}
        for key, value in annotations.items():
            if isinstance(value, QuestionAnnotation):
                result[key] = value.model_dump(by_alias=True, exclude_none=True)
            else:
                result[key] = value
        return result
    return annotations


# Singleton instance.
ask_user_question_tool = AskUserQuestionTool()
