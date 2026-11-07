"""Find and mask personal data in text, before it's logged, stored or sent to a model provider.

These patterns catch common Indian identifiers and contact details. They're a safety net, not a
guarantee: a name or a free-text address won't be caught.
"""
import logging
import re
import traceback
from collections.abc import Iterable
from typing import Any

STACK_FRAMES = 6  # the last few frames show where it failed, and a full LangChain stack fills the log
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
AADHAAR = re.compile(r"\b[2-9][0-9]{3}[ -]?[0-9]{4}[ -]?[0-9]{4}\b")
CARD = re.compile(r"\b[0-9](?:[ -]?[0-9]){12,18}\b")
PHONE = re.compile(r"(?<![\w-])(?:\+?91[ -]?|0)?[6-9][0-9]{4}[ -]?[0-9]{5}(?![\w-])")
IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
BANK_ACCOUNT = re.compile(r"(?i)\b((?:bank\s+)?(?:account|a/c|acct)(?:\s+(?:number|no\.?))?\s*(?:is|:|-)?\s*)"
                          r"([0-9]{9,18})\b")


def _luhn_valid(number: str) -> bool:
    digits = [int(character) for character in number if character.isdigit()]
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _mask_card(match: re.Match) -> str:
    return "[CARD]" if _luhn_valid(match[0]) else match[0]


def mask(text: str, keep: Iterable[str] = ()) -> str:
    """Replace personal data with a label saying what kind it was, so the sentence still makes sense.

    keep lists email addresses that are public, such as the HR helpdesk's.
    """
    if not text:
        return text
    public = {address.lower() for address in keep}
    masked = EMAIL.sub(lambda match: match[0] if match[0].lower() in public else "[EMAIL]", text)
    masked = PAN.sub("[PAN]", masked)
    masked = IFSC.sub("[IFSC]", masked)
    masked = CARD.sub(_mask_card, masked)
    masked = AADHAAR.sub("[AADHAAR]", masked)
    masked = BANK_ACCOUNT.sub(lambda match: f"{match[1]}[BANK_ACCOUNT]", masked)
    return PHONE.sub("[PHONE]", masked)


def mask_value(value: Any) -> Any:
    """Mask every string inside a value, such as tool arguments on their way to the audit log."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    return value


class MaskingFilter(logging.Filter):
    """Mask personal data in log records, including any exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask(record.getMessage())
        record.args = ()
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = mask(formatter.formatException(record.exc_info))
            # For structured logs: the kind of error and where, without its message, which can quote anything
            record.error_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            record.error_stack = traceback.format_tb(record.exc_info[2])[-STACK_FRAMES:]
            record.exc_info = None
        return True


def install_log_masking() -> None:
    """Apply masking to every log handler, so no part of the app can skip it by accident."""
    # uvicorn's access log is left out: it only holds paths, and its format needs the original arguments
    for logger in (logging.getLogger(), logging.getLogger("uvicorn")):
        for handler in logger.handlers:
            if not any(isinstance(item, MaskingFilter) for item in handler.filters):
                handler.addFilter(MaskingFilter())
