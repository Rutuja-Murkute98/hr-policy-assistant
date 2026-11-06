"""Defences against prompt injection. Retrieved text and uploads are data, never instructions.

The main defences are structural: tools are only offered on the self-service path, which never sees
retrieved text, and identity comes from the request. These helpers add three more layers: invisible
characters are removed, excerpts are clearly fenced in the prompt, and suspicious uploads are flagged.
"""
import re

INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
FENCE = re.compile(r"</?\s*excerpt\b[^>]*>", re.IGNORECASE)
INJECTION_SIGNS = {
    "ignore_instructions": r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
                           r"\b(?:instructions?|rules?|prompts?|above|guidelines?)\b",
    "reveal_prompt": r"\b(?:reveal|print|show|repeat|leak)\b[^.\n]{0,40}"
                     r"\b(?:system prompt|your (?:instructions|rules|prompt)|api key)\b",
    "role_override": r"\byou are now\b|\bact as\b[^.\n]{0,30}\b(?:system|admin|developer)\b"
                     r"|^\s*(?:system|assistant)\s*:",
    "tool_trigger": r"\b(?:call|use|invoke|run)\b[^.\n]{0,30}\b(?:tool|function)\b"
                    r"|\bapply_for_leave\b|\braise_hr_ticket\b|\bget_latest_payslip\b",
}
COMPILED = {name: re.compile(pattern, re.IGNORECASE | re.MULTILINE) for name, pattern in INJECTION_SIGNS.items()}


def strip_invisible(text: str) -> str:
    """Remove characters that can hide instructions from people but not from a model."""
    return INVISIBLE.sub("", text)


def fence_excerpt(text: str) -> str:
    """Stop an excerpt from closing its own fence in the prompt, so its text can't pose as instructions."""
    return FENCE.sub("[removed]", strip_invisible(text))


def find_injection_signs(text: str) -> list[str]:
    """Name the kinds of injection this text looks like. It's a signal for review, not proof."""
    cleaned = strip_invisible(text)
    return [name for name, pattern in COMPILED.items() if pattern.search(cleaned)]
