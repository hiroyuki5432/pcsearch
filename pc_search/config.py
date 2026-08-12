from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SearchConfig:
    config_path: Path
    database_path: Path
    inventory_report_path: Path
    roots: tuple[Path, ...]
    extensions: frozenset[str]
    exclude_folder_names: frozenset[str]
    exclude_folder_globs: tuple[str, ...]
    exclude_file_globs: tuple[str, ...]
    exclude_folder_paths: tuple[Path, ...]
    exclude_file_paths: frozenset[Path]
    file_policies: dict[str, str]
    table_head_rows: int
    max_file_size_bytes: int
    max_text_chars_per_file: int
    chunk_chars: int
    chunk_overlap_chars: int
    index_workers: int
    database_warning_bytes: int
    auto_index_interval_minutes: int
    host: str
    port: int

    @classmethod
    def load(cls, config_path: str | Path) -> "SearchConfig":
        path = Path(config_path).resolve()
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent

        def resolve(value: str) -> Path:
            candidate = Path(os.path.expandvars(value)).expanduser()
            return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()

        roots = tuple(resolve(value) for value in data["roots"])
        extensions = frozenset(
            value.lower() if value.startswith(".") else f".{value.lower()}"
            for value in data["extensions"]
        )
        return cls(
            config_path=path,
            database_path=resolve(data.get("database_path", "data/search_index_v2.db")),
            inventory_report_path=resolve(data.get("inventory_report_path", "data/inventory_report.json")),
            roots=roots,
            extensions=extensions,
            exclude_folder_names=frozenset(name.casefold() for name in data.get("exclude_folder_names", [])),
            exclude_folder_globs=tuple(data.get("exclude_folder_globs", [])),
            exclude_file_globs=tuple(data.get("exclude_file_globs", [])),
            exclude_folder_paths=tuple(resolve(value) for value in data.get("exclude_folder_paths", [])),
            exclude_file_paths=frozenset(resolve(value) for value in data.get("exclude_file_paths", [])),
            file_policies={
                str(resolve(value)): str(policy).lower()
                for value, policy in data.get("file_policies", {}).items()
                if str(policy).lower() in {"full", "head", "metadata", "exclude"}
            },
            table_head_rows=max(1, int(data.get("table_head_rows", 5000))),
            max_file_size_bytes=int(float(data.get("max_file_size_mb", 50)) * 1024 * 1024),
            max_text_chars_per_file=int(data.get("max_text_chars_per_file", 5_000_000)),
            chunk_chars=max(500, int(data.get("chunk_chars", 4_000))),
            chunk_overlap_chars=max(0, int(data.get("chunk_overlap_chars", 250))),
            index_workers=max(1, min(8, int(data.get("index_workers", 2)))),
            database_warning_bytes=int(float(data.get("database_warning_mb", 10_240)) * 1024 * 1024),
            auto_index_interval_minutes=max(0, int(data.get("auto_index_interval_minutes", 0))),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 8765)),
        )

    def is_excluded_dir_name(self, name: str) -> bool:
        folded = name.casefold()
        return folded in self.exclude_folder_names or any(
            fnmatch.fnmatch(folded, pattern.casefold())
            for pattern in self.exclude_folder_globs
        )

    def is_excluded_file(self, name: str) -> bool:
        folded = name.casefold()
        return any(fnmatch.fnmatch(folded, pattern.casefold()) for pattern in self.exclude_file_globs)

    def is_excluded_path(self, path: Path, *, directory: bool = False) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if not directory and resolved in self.exclude_file_paths:
            return True
        if not directory and self.file_policy(resolved) == "exclude":
            return True
        for excluded in self.exclude_folder_paths:
            try:
                resolved.relative_to(excluded)
                return True
            except ValueError:
                continue
        return False

    def file_policy(self, path: Path) -> str:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path.absolute())
        return self.file_policies.get(key, "full")

    def has_file_policy(self, path: Path) -> bool:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path.absolute())
        return key in self.file_policies

    @property
    def scope_hash(self) -> str:
        payload = {
            "roots": sorted(str(path).casefold() for path in self.roots),
            "extensions": sorted(self.extensions),
            "exclude_folder_names": sorted(self.exclude_folder_names),
            "exclude_folder_globs": list(self.exclude_folder_globs),
            "exclude_file_globs": list(self.exclude_file_globs),
            "exclude_folder_paths": sorted(str(path).casefold() for path in self.exclude_folder_paths),
            "exclude_file_paths": sorted(str(path).casefold() for path in self.exclude_file_paths),
            "excluded_file_policies": sorted(
                path.casefold() for path, value in self.file_policies.items() if value == "exclude"
            ),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]

    def extraction_hash(self, path: Path) -> str:
        payload = {
            "policy": self.file_policy(path),
            "max_file_size_bytes": self.max_file_size_bytes,
            "max_text_chars_per_file": self.max_text_chars_per_file,
            "chunk_chars": self.chunk_chars,
            "chunk_overlap_chars": self.chunk_overlap_chars,
            "table_head_rows": self.table_head_rows,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]


def update_config(config_path: Path, changes: dict[str, Any]) -> SearchConfig:
    """Atomically update supported user-editable settings and reload the config."""
    allowed = {
        "roots", "exclude_folder_names", "exclude_folder_globs", "exclude_file_globs",
        "exclude_folder_paths", "exclude_file_paths", "file_policies", "table_head_rows",
        "max_file_size_mb", "max_text_chars_per_file", "index_workers",
        "auto_index_interval_minutes",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"変更できない設定です: {', '.join(sorted(unknown))}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data.update(changes)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, config_path)
    return SearchConfig.load(config_path)
