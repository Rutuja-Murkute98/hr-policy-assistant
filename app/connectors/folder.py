"""A watched folder on the server, inside SOURCES_ROOT, in a folder named after the organisation. See D59."""
from pathlib import Path

from app.connectors.base import MAX_FILE_BYTES, ConnectorError, SourceFile, readable


def organisation_folder(root: Path, tenant_id: str, relative: str) -> Path:
    """The folder a source names, refused unless it's inside the organisation's own folder under the root."""
    home = (root / tenant_id).resolve()
    folder = (home / relative).resolve()
    if folder != home and home not in folder.parents:
        raise ConnectorError("The folder must be inside your organisation's own sources folder.")
    return folder


class FolderConnector:
    def __init__(self, folder: Path):
        self.folder = folder

    def list_files(self) -> list[SourceFile]:
        if not self.folder.is_dir():
            raise ConnectorError(f"The folder {self.folder.name} doesn't exist on the server.")
        files = []
        for path in sorted(self.folder.rglob("*")):
            # A link could point outside the folder, so only real files inside it are read
            if path.is_symlink() or not path.is_file() or not readable(path.name):
                continue
            stat = path.stat()
            files.append(SourceFile(path.relative_to(self.folder).as_posix(), path.name,
                                    f"{stat.st_mtime_ns}-{stat.st_size}"))
        return files

    def fetch(self, file: SourceFile) -> bytes:
        path = (self.folder / file.key).resolve()
        if self.folder.resolve() not in path.parents or not path.is_file():
            raise ConnectorError(f"{file.name} isn't in the folder any more.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ConnectorError(f"{file.name} is larger than the {MAX_FILE_BYTES // 1_000_000} MB limit.")
        return path.read_bytes()
