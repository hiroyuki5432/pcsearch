from __future__ import annotations

import sqlite3
import hashlib
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SearchConfig
from .text import fts_query, make_snippet


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    root_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    index_status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    content_bytes INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    extractor_version INTEGER NOT NULL DEFAULT 1,
    content_hash TEXT NOT NULL DEFAULT '',
    scope_hash TEXT NOT NULL DEFAULT '',
    extraction_hash TEXT NOT NULL DEFAULT '',
    is_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(index_status, is_deleted);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime_ns);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    location TEXT NOT NULL,
    content TEXT NOT NULL,
    UNIQUE(file_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content_terms,
    filename_terms,
    path_terms,
    content='',
    contentless_delete=1,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    indexed INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    pending_total INTEGER NOT NULL DEFAULT 0,
    scope_hash TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS index_queue (
    path TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    extraction_hash TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    queued_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_index_queue_scope ON index_queue(scope_hash, queued_at);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(config: SearchConfig, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = config.database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(config.database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def initialize(config: SearchConfig) -> None:
    with closing(connect(config)) as connection:
        connection.executescript(SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(files)")}
        if "extractor_version" not in columns:
            connection.execute(
                "ALTER TABLE files ADD COLUMN extractor_version INTEGER NOT NULL DEFAULT 1"
            )
        if "content_hash" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
        if "scope_hash" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN scope_hash TEXT NOT NULL DEFAULT ''")
        if "extraction_hash" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN extraction_hash TEXT NOT NULL DEFAULT ''")
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(index_runs)")}
        if "pending_total" not in run_columns:
            connection.execute("ALTER TABLE index_runs ADD COLUMN pending_total INTEGER NOT NULL DEFAULT 0")
        if "scope_hash" not in run_columns:
            connection.execute("ALTER TABLE index_runs ADD COLUMN scope_hash TEXT NOT NULL DEFAULT ''")
        scope_version = connection.execute(
            "SELECT value FROM app_metadata WHERE key='scope_hash_version'"
        ).fetchone()
        if not scope_version or scope_version[0] != "2":
            connection.execute(
                "UPDATE files SET scope_hash=? WHERE is_deleted=0",
                (config.scope_hash,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata(key,value) VALUES('scope_hash_version','2')"
            )
        else:
            connection.execute(
                "UPDATE files SET scope_hash=? WHERE scope_hash=''",
                (config.scope_hash,),
            )
        for row in connection.execute(
            "SELECT id,path FROM files WHERE extraction_hash=''"
        ):
            connection.execute(
                "UPDATE files SET extraction_hash=? WHERE id=?",
                (config.extraction_hash(Path(row["path"])), row["id"]),
            )
        # Older DBs did not store a duplicate hash. Backfill without rebuilding FTS.
        missing_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM files WHERE content_hash='' AND content_bytes>0"
            )
        ]
        for file_id in missing_ids:
            digest = hashlib.sha256()
            for row in connection.execute(
                "SELECT content FROM chunks WHERE file_id=? ORDER BY ordinal", (file_id,)
            ):
                digest.update((row[0] or "").encode("utf-8"))
                digest.update(b"\0")
            connection.execute(
                "UPDATE files SET content_hash=? WHERE id=?", (digest.hexdigest(), file_id)
            )
        connection.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES('schema_version', '5')"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files(content_hash, is_deleted)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_files_scope ON files(scope_hash, is_deleted)")
        connection.commit()


def _serialize_result(
    row: sqlite3.Row, query_tokens: list[str], match_kind: str = "content"
) -> dict[str, Any]:
    content = row["content"] or ""
    return {
        "file_id": row["file_id"],
        "chunk_id": row["chunk_id"],
        "filename": row["filename"],
        "path": row["path"],
        "extension": row["extension"],
        "size_bytes": row["size_bytes"],
        "mtime_ns": row["mtime_ns"],
        "location": row["location"],
        "content": content,
        "snippet": make_snippet(content, query_tokens),
        "score": round(float(row["score"]), 6),
        "match_kind": match_kind,
        "hit_count": int(row["hit_count"]),
        "duplicate_count": int(row["duplicate_count"]),
        "index_status": row["index_status"],
        "indexed_at": row["indexed_at"],
    }


def search(
    config: SearchConfig,
    query: str,
    *,
    mode: str = "and",
    extension: str = "",
    limit: int = 50,
    **filters: Any,
) -> list[dict[str, Any]]:
    return search_page(
        config, query, mode=mode, extension=extension, limit=limit, **filters
    )["results"]


def search_page(
    config: SearchConfig,
    query: str,
    *,
    mode: str = "and",
    extension: str = "",
    limit: int = 50,
    offset: int = 0,
    folder: str = "",
    statuses: tuple[str, ...] = (),
    content_only: bool = False,
    min_size: int = 0,
    max_size: int = 0,
    modified_after_ns: int = 0,
    modified_before_ns: int = 0,
    sort: str = "relevance",
) -> dict[str, Any]:
    if not config.database_path.exists():
        return {"results": [], "has_more": False, "offset": offset, "limit": limit}
    match, query_tokens = fts_query(query, mode)
    if not match:
        return {"results": [], "has_more": False, "offset": offset, "limit": limit}
    limit = max(1, min(int(limit), 100))
    offset = max(0, min(int(offset), 5000))
    requested_extensions = [value.strip().lower() for value in extension.split(",") if value.strip()]
    extensions = tuple(value if value.startswith(".") else f".{value}" for value in requested_extensions)
    allowed_statuses = {"ok", "empty", "error", "truncated", "too_large", "metadata_only", "unsupported"}
    statuses = tuple(value for value in statuses if value in allowed_statuses)

    conditions = ["f.is_deleted = 0", "f.scope_hash = ?"]
    condition_params: list[Any] = [config.scope_hash]
    if extensions:
        conditions.append(f"f.extension IN ({','.join('?' for _ in extensions)})")
        condition_params.extend(extensions)
    if folder:
        conditions.append("f.path LIKE ? ESCAPE '\\'")
        escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition_params.append(escaped.rstrip("\\/") + "\\\\%")
    if statuses:
        conditions.append(f"f.index_status IN ({','.join('?' for _ in statuses)})")
        condition_params.extend(statuses)
    if content_only:
        conditions.append("f.content_bytes > 0")
    if min_size > 0:
        conditions.append("f.size_bytes >= ?")
        condition_params.append(int(min_size))
    if max_size > 0:
        conditions.append("f.size_bytes <= ?")
        condition_params.append(int(max_size))
    if modified_after_ns > 0:
        conditions.append("f.mtime_ns >= ?")
        condition_params.append(int(modified_after_ns))
    if modified_before_ns > 0:
        conditions.append("f.mtime_ns <= ?")
        condition_params.append(int(modified_before_ns))
    where = " AND ".join(conditions)
    order = {
        "modified_desc": "mtime_ns DESC, score",
        "modified_asc": "mtime_ns ASC, score",
        "name": "filename COLLATE NOCASE, score",
        "size_desc": "size_bytes DESC, score",
    }.get(sort, "score")

    sql = f"""
        WITH raw_matches AS (
            SELECT
                f.id AS file_id,
                c.id AS chunk_id,
                f.filename,
                f.path,
                f.extension,
                f.size_bytes,
                f.mtime_ns,
                c.location,
                c.content,
                f.index_status,
                f.indexed_at,
                f.content_hash,
                bm25(chunks_fts, 1.0, 5.0, 2.0) AS score,
                1 AS hit_count
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN files f ON f.id = c.file_id
            WHERE chunks_fts MATCH ?
              AND {where}
        ), ranked_matches AS (
            SELECT
                raw_matches.*,
                ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY score) AS file_rank
            FROM raw_matches
        ), file_matches AS (
            SELECT * FROM ranked_matches WHERE file_rank = 1
        ), deduplicated AS (
            SELECT file_matches.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY CASE
                           WHEN content_hash='' THEN 'file:' || file_id
                           ELSE content_hash
                       END
                       ORDER BY score
                   ) AS duplicate_rank
            FROM file_matches
        )
        SELECT deduplicated.*,
               CASE WHEN content_hash='' THEN 1 ELSE (
                   SELECT COUNT(*) FROM files duplicates
                   WHERE duplicates.content_hash=deduplicated.content_hash
                     AND duplicates.scope_hash=? AND duplicates.is_deleted=0
               ) END AS duplicate_count
        FROM deduplicated
        WHERE duplicate_rank = 1
        ORDER BY {order}
        LIMIT ?
    """
    fetch_count = offset + limit + 1

    def execute(match_expression: str) -> list[sqlite3.Row]:
        params = [match_expression, *condition_params, config.scope_hash, fetch_count]
        return connection.execute(sql, params).fetchall()

    with closing(connect(config, readonly=True)) as connection:
        content_match = f"content_terms : ({match})"
        content_rows = execute(content_match)
        combined: list[tuple[sqlite3.Row, str]] = [(row, "content") for row in content_rows]
        seen = {row["file_id"] for row in content_rows}
        if not content_only and len(combined) < fetch_count:
            for row in execute(match):
                if row["file_id"] not in seen:
                    combined.append((row, "metadata"))
                    seen.add(row["file_id"])
                    if len(combined) >= fetch_count:
                        break

    page = combined[offset : offset + limit]
    return {
        "results": [_serialize_result(row, query_tokens, kind) for row, kind in page],
        "has_more": len(combined) > offset + limit,
        "offset": offset,
        "limit": limit,
    }


def status(config: SearchConfig) -> dict[str, Any]:
    if not config.database_path.exists():
        return {
            "database_exists": False,
            "database_bytes": 0,
            "files": 0,
            "chunks": 0,
            "errors": 0,
            "empty": 0,
            "truncated": 0,
            "too_large": 0,
            "metadata_only": 0,
            "unsupported": 0,
            "issue_signature": f"{config.scope_hash}|0",
            "last_run": None,
            "other_scope_files": 0,
            "resume_pending": 0,
        }
    with closing(connect(config, readonly=True)) as connection:
        totals = connection.execute(
            """
            SELECT
                COUNT(*) AS files,
                SUM(CASE WHEN index_status = 'error' THEN 1 ELSE 0 END) AS errors,
                SUM(CASE WHEN index_status = 'empty' THEN 1 ELSE 0 END) AS empty,
                SUM(CASE WHEN index_status = 'truncated' THEN 1 ELSE 0 END) AS truncated,
                SUM(CASE WHEN index_status = 'too_large' THEN 1 ELSE 0 END) AS too_large,
                SUM(CASE WHEN index_status = 'metadata_only' THEN 1 ELSE 0 END) AS metadata_only,
                SUM(CASE WHEN index_status = 'unsupported' THEN 1 ELSE 0 END) AS unsupported,
                MAX(CASE WHEN index_status <> 'ok' THEN indexed_at ELSE '' END) AS issue_updated_at
            FROM files WHERE is_deleted = 0 AND scope_hash=?
            """,
            (config.scope_hash,),
        ).fetchone()
        chunk_count = connection.execute(
            "SELECT COUNT(*) FROM chunks c JOIN files f ON f.id=c.file_id WHERE f.is_deleted=0 AND f.scope_hash=?",
            (config.scope_hash,),
        ).fetchone()[0]
        other_scope_files = connection.execute(
            "SELECT COUNT(*) FROM files WHERE is_deleted=0 AND scope_hash<>?",
            (config.scope_hash,),
        ).fetchone()[0]
        resume_pending = connection.execute(
            "SELECT COUNT(*) FROM index_queue WHERE scope_hash=?",
            (config.scope_hash,),
        ).fetchone()[0]
        last_run = connection.execute(
            "SELECT * FROM index_runs WHERE scope_hash IN ('', ?) ORDER BY id DESC LIMIT 1",
            (config.scope_hash,),
        ).fetchone()
    issue_signature = "|".join(
        str(value)
        for value in (
            config.scope_hash,
            totals["errors"] or 0,
            totals["empty"] or 0,
            totals["truncated"] or 0,
            totals["too_large"] or 0,
            totals["metadata_only"] or 0,
            totals["unsupported"] or 0,
            totals["issue_updated_at"] or "",
        )
    )
    return {
        "database_exists": True,
        "database_bytes": config.database_path.stat().st_size,
        "files": totals["files"] or 0,
        "chunks": chunk_count,
        "errors": totals["errors"] or 0,
        "empty": totals["empty"] or 0,
        "truncated": totals["truncated"] or 0,
        "too_large": totals["too_large"] or 0,
        "metadata_only": totals["metadata_only"] or 0,
        "unsupported": totals["unsupported"] or 0,
        "issue_signature": issue_signature,
        "last_run": dict(last_run) if last_run else None,
        "other_scope_files": other_scope_files,
        "resume_pending": resume_pending,
        "scope_hash": config.scope_hash,
    }


def extraction_issues(
    config: SearchConfig, *, status_filter: str = "", limit: int = 200
) -> list[dict[str, Any]]:
    if not config.database_path.exists():
        return []
    limit = max(1, min(int(limit), 1000))
    params: list[Any] = [config.scope_hash]
    status_sql = ""
    if status_filter:
        status_sql = " AND index_status=?"
        params.append(status_filter)
    params.append(limit)
    with closing(connect(config, readonly=True)) as connection:
        rows = connection.execute(
            f"""
            SELECT id AS file_id, filename, path, extension, size_bytes, indexed_at,
                   index_status, error, content_bytes, chunk_count
            FROM files
            WHERE is_deleted=0 AND scope_hash=? AND index_status<>'ok'{status_sql}
            ORDER BY CASE index_status
                WHEN 'error' THEN 0 WHEN 'too_large' THEN 1 WHEN 'empty' THEN 2
                WHEN 'truncated' THEN 3 ELSE 4 END, size_bytes DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def file_hits(
    config: SearchConfig, file_id: int, query: str, mode: str = "and", limit: int = 100
) -> list[dict[str, Any]]:
    match, tokens = fts_query(query, mode)
    if not match or not config.database_path.exists():
        return []
    limit = max(1, min(int(limit), 200))
    with closing(connect(config, readonly=True)) as connection:
        rows = connection.execute(
            """
            SELECT c.id AS chunk_id, c.ordinal, c.location, c.content,
                   bm25(chunks_fts, 1.0, 5.0, 2.0) AS score
            FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
            JOIN files f ON f.id=c.file_id
            WHERE chunks_fts MATCH ? AND f.id=? AND f.scope_hash=? AND f.is_deleted=0
            ORDER BY score LIMIT ?
            """,
            (f"content_terms : ({match})", file_id, config.scope_hash, limit),
        ).fetchall()
    return [
        {
            "chunk_id": row["chunk_id"],
            "ordinal": row["ordinal"],
            "location": row["location"],
            "content": row["content"],
            "snippet": make_snippet(row["content"], tokens),
        }
        for row in rows
    ]


def duplicate_paths(config: SearchConfig, file_id: int) -> list[dict[str, Any]]:
    if not config.database_path.exists():
        return []
    with closing(connect(config, readonly=True)) as connection:
        row = connection.execute(
            "SELECT content_hash FROM files WHERE id=? AND scope_hash=? AND is_deleted=0",
            (file_id, config.scope_hash),
        ).fetchone()
        if not row or not row["content_hash"]:
            return []
        rows = connection.execute(
            """
            SELECT id AS file_id, filename, path, extension, size_bytes
            FROM files WHERE content_hash=? AND scope_hash=? AND is_deleted=0
            ORDER BY path
            """,
            (row["content_hash"], config.scope_hash),
        ).fetchall()
    return [dict(value) for value in rows]


def path_impact(config: SearchConfig, path: str, *, is_file: bool = False) -> dict[str, Any]:
    if not config.database_path.exists():
        return {"files": 0, "content_bytes": 0, "source_bytes": 0, "path": path}
    with closing(connect(config, readonly=True)) as connection:
        if is_file:
            row = connection.execute(
                """
                SELECT COUNT(*) AS files, COALESCE(SUM(content_bytes),0) AS content_bytes,
                       COALESCE(SUM(size_bytes),0) AS source_bytes
                FROM files WHERE is_deleted=0 AND scope_hash=? AND path=?
                """,
                (config.scope_hash, path),
            ).fetchone()
        else:
            prefix = path.rstrip("\\/") + "\\%"
            row = connection.execute(
                """
                SELECT COUNT(*) AS files, COALESCE(SUM(content_bytes),0) AS content_bytes,
                       COALESCE(SUM(size_bytes),0) AS source_bytes
                FROM files WHERE is_deleted=0 AND scope_hash=? AND path LIKE ?
                """,
                (config.scope_hash, prefix),
            ).fetchone()
    return {"path": path, **dict(row)}


def exclusion_suggestions(config: SearchConfig, limit: int = 30) -> list[dict[str, Any]]:
    """Return review-only folder suggestions; never changes the config automatically."""
    if not config.database_path.exists():
        return []
    markers = {
        "site-packages": "Pythonライブラリ",
        "portablegit": "同梱Gitツール",
        "node_modules": "Node.jsライブラリ",
        ".gradle": "Gradleキャッシュ",
        ".cache": "キャッシュ",
        "dist-info": "パッケージ情報",
        "__pycache__": "Pythonキャッシュ",
        "backup": "バックアップ候補",
        "backups": "バックアップ候補",
        "バックアップ": "バックアップ候補",
    }
    with closing(connect(config, readonly=True)) as connection:
        rows = connection.execute(
            """
            SELECT path, root_path, size_bytes, content_bytes
            FROM files WHERE is_deleted=0 AND scope_hash=?
            """,
            (config.scope_hash,),
        ).fetchall()
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = Path(row["path"])
        root = Path(row["root_path"])
        try:
            relative_parts = path.parent.relative_to(root).parts
        except ValueError:
            relative_parts = path.parent.parts
        matched: tuple[str, str] | None = None
        for index, part in enumerate(relative_parts):
            folded = part.casefold()
            reason = markers.get(folded)
            if reason or folded.endswith("_backup") or folded.endswith("_bk"):
                matched = (str(root.joinpath(*relative_parts[: index + 1])), reason or "バックアップ候補")
                break
        if not matched:
            continue
        folder, reason = matched
        item = totals.setdefault(
            folder,
            {"path": folder, "reason": reason, "files": 0, "source_bytes": 0, "content_bytes": 0},
        )
        item["files"] += 1
        item["source_bytes"] += row["size_bytes"]
        item["content_bytes"] += row["content_bytes"]
    return sorted(totals.values(), key=lambda value: value["content_bytes"], reverse=True)[:limit]


def storage_breakdown(
    config: SearchConfig, limit: int = 15, parent: str = ""
) -> dict[str, Any]:
    """Calculate storage consumers on demand; this is never called by search."""
    if not config.database_path.exists():
        return {
            "database_bytes": 0,
            "source_bytes": 0,
            "content_bytes": 0,
            "index_overhead_bytes": 0,
            "files": 0,
            "chunks": 0,
            "top_files": [],
            "top_folders": [],
        }

    limit = max(1, min(int(limit), 50))
    with closing(connect(config, readonly=True)) as connection:
        rows = connection.execute(
            """
            SELECT id, path, root_path, filename, extension, size_bytes,
                   content_bytes, chunk_count, index_status
            FROM files
            WHERE is_deleted = 0 AND scope_hash=?
            """,
            (config.scope_hash,),
        ).fetchall()
        actual_chunks = connection.execute(
            "SELECT COUNT(*) FROM chunks c JOIN files f ON f.id=c.file_id WHERE f.is_deleted=0 AND f.scope_hash=?",
            (config.scope_hash,),
        ).fetchone()[0]

    selected_parent = Path(parent).resolve() if parent else None
    if selected_parent:
        scoped_rows = []
        for row in rows:
            try:
                Path(row["path"]).resolve().relative_to(selected_parent)
                scoped_rows.append(row)
            except ValueError:
                continue
        rows = scoped_rows

    source_bytes = sum(row["size_bytes"] for row in rows)
    content_bytes = sum(row["content_bytes"] for row in rows)
    chunks = actual_chunks
    database_bytes = config.database_path.stat().st_size

    top_files = []
    for row in sorted(rows, key=lambda item: item["content_bytes"], reverse=True)[:limit]:
        top_files.append(
            {
                "file_id": row["id"],
                "name": row["filename"],
                "path": row["path"],
                "extension": row["extension"],
                "source_bytes": row["size_bytes"],
                "content_bytes": row["content_bytes"],
                "chunks": row["chunk_count"],
                "status": row["index_status"],
                "content_percent": round(row["content_bytes"] * 100 / max(content_bytes, 1), 2),
            }
        )

    folder_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"files": 0, "source_bytes": 0, "content_bytes": 0, "chunks": 0}
    )
    for row in rows:
        path = Path(row["path"])
        root = Path(row["root_path"])
        base = selected_parent or root
        try:
            parts = path.parent.relative_to(base).parts
        except ValueError:
            continue
        folder = str(base.joinpath(parts[0])) if parts else str(base)
        totals = folder_totals[folder]
        totals["files"] += 1
        totals["source_bytes"] += row["size_bytes"]
        totals["content_bytes"] += row["content_bytes"]
        totals["chunks"] += row["chunk_count"]

    top_folders = []
    for folder, totals in sorted(
        folder_totals.items(), key=lambda item: item[1]["content_bytes"], reverse=True
    )[:limit]:
        top_folders.append(
            {
                "name": Path(folder).name,
                "path": folder,
                **totals,
                "content_percent": round(totals["content_bytes"] * 100 / max(content_bytes, 1), 2),
            }
        )

    return {
        "database_bytes": database_bytes,
        "source_bytes": source_bytes,
        "content_bytes": content_bytes,
        "index_overhead_bytes": max(0, database_bytes - content_bytes),
        "files": len(rows),
        "chunks": chunks,
        "top_files": top_files,
        "top_folders": top_folders,
        "parent": str(selected_parent) if selected_parent else "",
        "notes": [
            "本文量は検索DBへ保存した抽出テキストのUTF-8容量です。",
            "フォルダは現在位置の直下単位で集計しているため、表示行どうしは重複しません。",
            "検索索引・管理情報はDB全体から抽出本文量を引いた概算です。",
        ],
    }


def indexed_path_for_file_id(config: SearchConfig, file_id: int) -> Path | None:
    if not config.database_path.exists():
        return None
    with closing(connect(config, readonly=True)) as connection:
        row = connection.execute(
            "SELECT path FROM files WHERE id = ? AND is_deleted = 0 AND scope_hash=?",
            (file_id, config.scope_hash),
        ).fetchone()
    return Path(row["path"]) if row else None
