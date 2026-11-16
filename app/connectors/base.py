"""What every source of policies offers: list its files, fetch one, and see what changed. See docs/DECISIONS.md, D59."""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipeline.extract import SUPPORTED_TYPES

MAX_FILE_BYTES = 5_000_000  # the same as an upload


@dataclass(frozen=True)
class SourceFile:
    key: str  # what the source calls it, which stays the same when it's renamed or edited where possible
    name: str  # its file name, with the extension the reader goes by
    version: str  # changes whenever its content does: a checksum, an ETag, or a time and size


class ConnectorError(Exception):
    """The source couldn't be read. The message is safe to show an HR admin."""


class Connector(Protocol):
    def list_files(self) -> list[SourceFile]:
        """Every file at the source that the helpdesk can read."""

    def fetch(self, file: SourceFile) -> bytes:
        """The file's content."""


def changes(files: list[SourceFile], known: dict[str, str]) -> tuple[list[SourceFile], list[str]]:
    """The files that are new or changed since the versions known, and the keys of known files now gone."""
    current = {file.key for file in files}
    changed = [file for file in files if known.get(file.key) != file.version]
    return changed, [key for key in known if key not in current]


def readable(name: str) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_TYPES


def policy_name(name: str) -> str:
    """A file name as a policy name, such as "Leave Policy (2026).pdf" to "Leave_Policy_2026.pdf"."""
    path = Path(name)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem).strip("_-")
    return f"{stem or 'Document'}{path.suffix.lower()}"
