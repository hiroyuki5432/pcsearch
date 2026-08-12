from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import SearchConfig


@dataclass(frozen=True)
class CandidateFile:
    path: Path
    root: Path
    extension: str
    size: int
    mtime_ns: int


def iter_candidate_files(config: SearchConfig) -> Iterator[CandidateFile]:
    for root in config.roots:
        if not root.is_dir():
            continue
        for current_root, dirs, files in os.walk(root):
            current = Path(current_root)
            dirs[:] = [
                name for name in dirs
                if not config.is_excluded_dir_name(name)
                and not config.is_excluded_path(current / name, directory=True)
            ]
            for name in files:
                if config.is_excluded_file(name):
                    continue
                extension = Path(name).suffix.lower()
                if extension not in config.extensions:
                    continue
                path = current / name
                if config.is_excluded_path(path):
                    continue
                try:
                    stat = path.stat()
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                if not stat_module.S_ISREG(stat.st_mode):
                    continue
                yield CandidateFile(
                    path=path,
                    root=root,
                    extension=extension,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
