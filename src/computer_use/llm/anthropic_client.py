"""Anthropic Claude implementation of LLMClient.

Deliberately does NOT use Anthropic's native "computer use" tool (which is
screenshot + raw pixel coordinates). That tool would push the model toward
coordinate targeting as its default vocabulary, which contradicts the
semantic-locator-first strategy this project uses. Instead the model is given
a small custom tool schema -- click/fill/navigate/extract/wait plus two
terminal signals -- and describes *targets* in role/accessible-name terms (how
a screen reader -- or a human operator -- would refer to a control), reading
from the same SurfaceState the replay engine's locator resolver understands.
The model never sees or produces a Playwright selector.
"""
from __future__ import annotations

import json
import os

from anthropic import Anthropic

from computer_use.agent.types import AgentDecision, DecisionKind
from computer_use.llm.base import LLMClient
from computer_use.models.actions import Action, ActionType, Locator, LocatorStrategy, Target
from computer_use.models.surface_state import SurfaceState

_SYSTEM_PROMPT = """You are a careful back-office banking automation operator.
You interact with a legacy web application one step at a time. You are given
the current page's URL, title, a short text excerpt, and a list of visible
interactive/labelled elements (each with a role and, where available, an
accessible name). You must choose exactly ONE tool call representing the next
single action toward the stated goal.

Rules:
- Prefer acting on elements by their role and accessible name -- refer to
  controls the way a person reading the screen would (e.g. role="button",
  name="Search"), never by pixel position.
- Take one step at a time. After each action you will be shown the new state.
- If the page shows a business result (e.g. "no such member", a validation
  message, an access-denied notice), that is a normal outcome of the goal,
  not a reason to keep retrying -- call finish_success and summarize what you
  found, or finish_stuck only if you genuinely cannot proceed.
- If you cannot find a control to make progress after reviewing the element
  list, and no more information would help, call finish_stuck with your
  reasoning rather than guessing.
- Never invent data. Only use values present in the goal or the page.

When you call finish_success, you must also name the REUSABLE CAPABILITY this
run demonstrates -- not a transcript of this specific run. Abstract away any
concrete identifier, amount, or other caller-supplied value from the goal:
- capability_slug: lowercase_snake_case, generic, no specific IDs/values.
  Goal "Look up member 1008 and read the savings balance" -> slug
  "lookup_member_savings_balance", NOT "look_up_member_1008_and_read_..."
- capability_name: short human-readable title, same generalization, e.g.
  "Look up member savings balance".
- capability_description: one sentence describing what the capability does
  in general terms, e.g. "Looks up a member by ID and returns their savings
  balance." Never mention the specific ID/value used in this run.
"""

_TOOLS = [
    {
        "name": "click",
        "description": "Click a control identified by its accessibility role and accessible name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "e.g. button, link, checkbox"},
                "name": {"type": "string", "description": "accessible name / visible label of the control"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "reasoning"],
        },
    },
    {
        "name": "fill",
        "description": "Type text into a labelled input/textbox identified by role and accessible name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "usually 'textbox'"},
                "name": {"type": "string", "description": "accessible name / label / placeholder of the field"},
                "value": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "value", "reasoning"],
        },
    },
    {
        "name": "navigate",
        "description": "Navigate the browser directly to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "reasoning": {"type": "string"}},
            "required": ["url", "reasoning"],
        },
    },
    {
        "name": "extract",
        "description": "Read the text of an element identified by role and accessible name, and store it under output_key.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "output_key": {"type": "string", "description": "e.g. savings_balance"},
                "reasoning": {"type": "string"},
            },
            "required": ["role", "name", "output_key", "reasoning"],
        },
    },
    {
        "name": "wait",
        "description": "Wait briefly, e.g. after observing the page is still loading.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}, "reasoning": {"type": "string"}},
            "required": ["seconds", "reasoning"],
        },
    },
    {
        "name": "finish_success",
        "description": "Call this once the goal has been achieved (including a legitimate business outcome like 'not found').",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Human-readable summary of what happened in THIS run."},
                "capability_slug": {
                    "type": "string",
                    "description": "Generic lowercase_snake_case id for the reusable capability, with all specific IDs/values abstracted away.",
                },
                "capability_name": {
                    "type": "string",
                    "description": "Short generic human-readable capability title, e.g. 'Look up member savings balance'.",
                },
                "capability_description": {
                    "type": "string",
                    "description": "One generic sentence describing what the capability does, no specific values.",
                },
                "reasoning": {"type": "string"},
            },
            "required": ["summary", "capability_slug", "capability_name", "capability_description", "reasoning"],
        },
    },
    {
        "name": "finish_stuck",
        "description": "Call this if you cannot safely make further progress toward the goal.",
        "input_schema": {
            "type": "object",
            "properties": {"reasoning": {"type": "string"}},
            "required": ["reasoning"],
        },
    },
]


def _render_state(state: SurfaceState) -> str:
    lines = [f"URL: {state.url}", f"Title: {state.title}", ""]
    if state.visible_text_excerpt:
        lines.append("Visible text (excerpt):")
        lines.append(state.visible_text_excerpt[:1500])
        lines.append("")
    lines.append("Interactive/labelled elements:")
    for el in state.elements:
        if not (el.role and (el.name or el.text)):
            continue
        descriptor = f"- role={el.role}"
        if el.name:
            descriptor += f' name="{el.name}"'
        if el.text and el.text != el.name:
            descriptor += f' text="{el.text[:80]}"'
        lines.append(descriptor)
    return "\n".join(lines)


class AnthropicLLMClient(LLMClient):
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self._client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self._model = model or os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

    def decide(self, *, goal: str, state: SurfaceState, history: list[str]) -> AgentDecision:
        history_block = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history[-10:])) or "(no actions taken yet)"
        user_content = (
            f"GOAL: {goal}\n\n"
            f"ACTIONS TAKEN SO FAR:\n{history_block}\n\n"
            f"CURRENT PAGE STATE:\n{_render_state(state)}\n\n"
            "Choose exactly one tool call for your next step."
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            tools=_TOOLS,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_content}],
        )

        for block in response.content:
            if block.type == "tool_use":
                return _decision_from_tool_call(block.name, block.input)

        return AgentDecision(kind=DecisionKind.FINISH_STUCK, reasoning="model returned no tool call")


def _decision_from_tool_call(name: str, args: dict) -> AgentDecision:
    reasoning = args.get("reasoning", "")

    if name == "finish_success":
        return AgentDecision(
            kind=DecisionKind.FINISH_SUCCESS,
            reasoning=reasoning,
            summary=args.get("summary"),
            capability_slug=args.get("capability_slug"),
            capability_name=args.get("capability_name"),
            capability_description=args.get("capability_description"),
        )
    if name == "finish_stuck":
        return AgentDecision(kind=DecisionKind.FINISH_STUCK, reasoning=reasoning)

    if name == "navigate":
        action = Action(type=ActionType.NAVIGATE, value=args["url"], reasoning=reasoning)
    elif name == "wait":
        action = Action(type=ActionType.WAIT, value=str(args.get("seconds", 1)), reasoning=reasoning)
    elif name in ("click", "fill", "extract"):
        role = args.get("role", "")
        target_name = args.get("name", "")
        target = Target(
            primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value=f"{role}|{target_name}", confidence="high"),
            fallbacks=[
                Locator(strategy=LocatorStrategy.SEMANTIC, value=target_name, confidence="medium"),
            ] if target_name else [],
        )
        action_type = {"click": ActionType.CLICK, "fill": ActionType.FILL, "extract": ActionType.EXTRACT}[name]
        value = args.get("value") if name == "fill" else (args.get("output_key") if name == "extract" else None)
        action = Action(type=action_type, target=target, value=value, reasoning=reasoning)
    else:
        return AgentDecision(kind=DecisionKind.FINISH_STUCK, reasoning=f"unknown tool call '{name}': {json.dumps(args)}")

    return AgentDecision(kind=DecisionKind.ACTION, action=action, reasoning=reasoning)
