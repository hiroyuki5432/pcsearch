from __future__ import annotations

import os
import multiprocessing
import hashlib
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import SearchConfig
from .database import connect, initialize, utc_now
from .extractors import extract_file
from .files import CandidateFile, iter_candidate_files
from .models import CancellationRequested, PreparedIndexFile, TextChunk
from .text import terms_text


ProgressCallback = Callable[[dict[str, int | str | bool]], None]
CancelCheck = Callable[[], bool]
EXTRACTOR_VERSION = 3


@dataclass
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0
    deleted: int = 0
    total: int = 0
    pending_total: int = 0
    workers: int = 1
    phase: str = "scanning"
    cancelled: bool = False
    current_path: str = ""
    mode: str = "scan"
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int | str | bool]:
        return {
            "scanned": self.scanned,
            "indexed": self.indexed,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "deleted": self.deleted,
            "total": self.total,
            "pending_total": self.pending_total,
            "workers": self.workers,
            "phase": self.phase,
            "cancelled": self.cancelled,
            "current_path": self.current_path,
            "mode": self.mode,
        }


_EXTENSION_PRIORITY = {
    ".txt": 0,
    ".docx": 0,
    ".doc": 0,
    ".pptx": 0,
    ".ppt": 0,
    ".pdf": 1,
    ".xls": 2,
    ".xlsx": 3,
    ".csv": 4,
}


def _candidate_priority(candidate: CandidateFile) -> tuple[int, int, str]:
    return (
        _EXTENSION_PRIORITY.get(candidate.extension, 5),
        candidate.size,
        str(candidate.path).casefold(),
    )


def _prepare_candidate(candidate: CandidateFile, config: SearchConfig, cancel_signal=None) -> PreparedIndexFile:
    should_cancel = cancel_signal.is_set if cancel_signal is not None else None
    result = extract_file(candidate.path, config, should_cancel=should_cancel)
    chunks = result.chunks or [TextChunk(location="メタデータのみ", content="")]
    content_terms: list[str] = []
    for chunk in chunks:
        if should_cancel and should_cancel():
            raise CancellationRequested()
        content_terms.append(terms_text(chunk.content))
    digest = hashlib.sha256()
    if result.chunks:
        for chunk in result.chunks:
            digest.update(chunk.content.encode("utf-8"))
            digest.update(b"\0")
    return PreparedIndexFile(
        result=result,
        content_terms=content_terms,
        filename_terms=terms_text(candidate.path.name),
        path_terms=terms_text(str(candidate.path.parent)),
        content_hash=digest.hexdigest() if result.chunks else "",
    )


def _delete_chunks(connection, file_id: int) -> None:
    chunk_ids = [
        row[0]
        for row in connection.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))
    ]
    if chunk_ids:
        connection.executemany("DELETE FROM chunks_fts WHERE rowid = ?", ((value,) for value in chunk_ids))
    connection.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))


def _write_file(
    connection,
    candidate: CandidateFile,
    prepared: PreparedIndexFile,
    config: SearchConfig,
    should_cancel: CancelCheck | None = None,
) -> None:
    result = prepared.result
    indexed_at = utc_now()
    connection.execute(
        """
        INSERT INTO files(
            path, root_path, filename, extension, size_bytes, mtime_ns,
            indexed_at, index_status, error, content_bytes, chunk_count,
            extractor_version, is_deleted
            , content_hash, scope_hash, extraction_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            root_path=excluded.root_path,
            filename=excluded.filename,
            extension=excluded.extension,
            size_bytes=excluded.size_bytes,
            mtime_ns=excluded.mtime_ns,
            indexed_at=excluded.indexed_at,
            index_status=excluded.index_status,
            error=excluded.error,
            content_bytes=excluded.content_bytes,
            chunk_count=excluded.chunk_count,
            extractor_version=excluded.extractor_version,
            content_hash=excluded.content_hash,
            scope_hash=excluded.scope_hash,
            extraction_hash=excluded.extraction_hash,
            is_deleted=0
        """,
        (
            str(candidate.path),
            str(candidate.root),
            candidate.path.name,
            candidate.extension,
            candidate.size,
            candidate.mtime_ns,
            indexed_at,
            result.status,
            result.error,
            result.content_bytes,
            len(result.chunks),
            EXTRACTOR_VERSION,
            prepared.content_hash,
            config.scope_hash,
            config.extraction_hash(candidate.path),
        ),
    )
    file_id = connection.execute("SELECT id FROM files WHERE path = ?", (str(candidate.path),)).fetchone()[0]
    _delete_chunks(connection, file_id)

    chunks = result.chunks or [TextChunk(location="メタデータのみ", content="")]
    for ordinal, (chunk, content_terms) in enumerate(zip(chunks, prepared.content_terms, strict=True)):
        if should_cancel and should_cancel():
            raise CancellationRequested()
        cursor = connection.execute(
            "INSERT INTO chunks(file_id, ordinal, location, content) VALUES (?, ?, ?, ?)",
            (file_id, ordinal, chunk.location, chunk.content),
        )
        chunk_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO chunks_fts(rowid, content_terms, filename_terms, path_terms) VALUES (?, ?, ?, ?)",
            (chunk_id, content_terms, prepared.filename_terms, prepared.path_terms),
        )


def run_index(
    config: SearchConfig,
    *,
    limit: int | None = None,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    mode: str = "scan",
) -> IndexStats:
    if mode not in {"scan", "resume"}:
        raise ValueError("mode must be 'scan' or 'resume'")
    initialize(config)
    stats = IndexStats(mode=mode, phase="resuming" if mode == "resume" else "scanning")
    seen_paths: set[str] = set()

    def cancellation_point() -> None:
        if should_cancel and should_cancel():
            raise CancellationRequested()

    def report() -> None:
        if progress:
            progress(stats.as_dict())

    with closing(connect(config)) as connection:
        # A forced shutdown cannot enter the exception handler. Close any stale
        # run before starting a new one so the UI does not report it forever.
        now = utc_now()
        connection.execute(
            """
            UPDATE index_runs
            SET finished_at=?, status='interrupted',
                message=CASE
                    WHEN message = '' THEN '前回の索引作成は完了前に終了しました'
                    ELSE message || char(10) || '前回の索引作成は完了前に終了しました'
                END
            WHERE status='running'
            """,
            (now,),
        )
        run_id = connection.execute(
            "INSERT INTO index_runs(started_at, status, scope_hash) VALUES (?, 'running', ?)",
            (now, config.scope_hash),
        ).lastrowid
        connection.commit()

        try:
            pending: list[CandidateFile] = []
            if mode == "scan":
                # Keep the current scope's prior queue until this replacement scan
                # completes. A second crash must not discard resumable work.
                connection.execute(
                    "DELETE FROM index_queue WHERE scope_hash<>?", (config.scope_hash,)
                )
                connection.commit()
                existing_rows = connection.execute(
                    """
                    SELECT id, path, size_bytes, mtime_ns, extractor_version, is_deleted,
                           scope_hash, extraction_hash
                    FROM files WHERE is_deleted=0
                    """
                ).fetchall()
                existing_by_path = {
                    os.path.normcase(os.path.normpath(row["path"])): row
                    for row in existing_rows
                }

                # This pass only reads directory entries and file metadata. Excluded
                # cloud placeholders are filtered before any file content is opened.
                for candidate in iter_candidate_files(config):
                    cancellation_point()
                    if limit is not None and stats.scanned >= max(0, limit):
                        break
                    normalized_path = os.path.normcase(os.path.normpath(str(candidate.path)))
                    seen_paths.add(normalized_path)
                    stats.scanned += 1
                    stats.current_path = str(candidate.path)
                    existing = existing_by_path.get(normalized_path)
                    extraction_hash = config.extraction_hash(candidate.path)
                    if (
                        existing
                        and existing["size_bytes"] == candidate.size
                        and existing["mtime_ns"] == candidate.mtime_ns
                        and existing["extractor_version"] == EXTRACTOR_VERSION
                        and existing["is_deleted"] == 0
                        and existing["extraction_hash"] == extraction_hash
                    ):
                        if existing["scope_hash"] != config.scope_hash:
                            connection.execute(
                                "UPDATE files SET scope_hash=? WHERE id=?",
                                (config.scope_hash, existing["id"]),
                            )
                        connection.execute(
                            "DELETE FROM index_queue WHERE path=?", (str(candidate.path),)
                        )
                        stats.unchanged += 1
                    else:
                        pending.append(candidate)
                        connection.execute(
                            """
                            INSERT INTO index_queue(
                                path, root_path, extension, size_bytes, mtime_ns,
                                extraction_hash, scope_hash, queued_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                root_path=excluded.root_path,
                                extension=excluded.extension,
                                size_bytes=excluded.size_bytes,
                                mtime_ns=excluded.mtime_ns,
                                extraction_hash=excluded.extraction_hash,
                                scope_hash=excluded.scope_hash,
                                queued_at=excluded.queued_at
                            """,
                            (
                                str(candidate.path), str(candidate.root), candidate.extension,
                                candidate.size, candidate.mtime_ns, extraction_hash,
                                config.scope_hash, utc_now(),
                            ),
                        )
                    if stats.scanned == 1 or stats.scanned % 50 == 0:
                        report()
                        connection.execute(
                            "UPDATE index_runs SET scanned=?, unchanged=?, message=? WHERE id=?",
                            (stats.scanned, stats.unchanged, f"確認中: {stats.current_path}", run_id),
                        )
                        connection.commit()
                stats.total = stats.scanned
                pending.sort(key=_candidate_priority)
            else:
                queue_rows = connection.execute(
                    """
                    SELECT path, root_path, extension, size_bytes, mtime_ns
                    FROM index_queue WHERE scope_hash=? ORDER BY queued_at, path
                    """,
                    (config.scope_hash,),
                ).fetchall()
                stats.total = len(queue_rows)
                for row in queue_rows:
                    cancellation_point()
                    path = Path(row["path"])
                    stats.scanned += 1
                    stats.current_path = str(path)
                    try:
                        file_stat = path.stat()
                    except (FileNotFoundError, PermissionError, OSError):
                        with connection:
                            connection.execute("DELETE FROM index_queue WHERE path=?", (str(path),))
                            old = connection.execute(
                                "SELECT id FROM files WHERE path=? AND is_deleted=0", (str(path),)
                            ).fetchone()
                            if old:
                                _delete_chunks(connection, old["id"])
                                connection.execute(
                                    "UPDATE files SET is_deleted=1, indexed_at=? WHERE id=?",
                                    (utc_now(), old["id"]),
                                )
                                stats.deleted += 1
                        continue
                    candidate = CandidateFile(
                        path=path,
                        root=Path(row["root_path"]),
                        extension=path.suffix.lower(),
                        size=file_stat.st_size,
                        mtime_ns=file_stat.st_mtime_ns,
                    )
                    pending.append(candidate)
                    connection.execute(
                        """
                        UPDATE index_queue SET extension=?, size_bytes=?, mtime_ns=?,
                            extraction_hash=?, queued_at=? WHERE path=?
                        """,
                        (
                            candidate.extension, candidate.size, candidate.mtime_ns,
                            config.extraction_hash(path), utc_now(), str(path),
                        ),
                    )
                    if stats.scanned == 1 or stats.scanned % 100 == 0:
                        report()
                        connection.commit()

            stats.phase = "indexing"
            stats.pending_total = len(pending)
            stats.workers = min(config.index_workers, max(1, len(pending)))
            connection.execute(
                "UPDATE index_runs SET pending_total=? WHERE id=?",
                (stats.pending_total, run_id),
            )
            connection.commit()
            report()

            def store(candidate: CandidateFile, prepared: PreparedIndexFile) -> None:
                cancellation_point()
                stats.current_path = str(candidate.path)
                with connection:
                    _write_file(connection, candidate, prepared, config, should_cancel)
                    connection.execute("DELETE FROM index_queue WHERE path=?", (str(candidate.path),))
                stats.indexed += 1
                result = prepared.result
                if result.status == "error":
                    stats.failed += 1
                    stats.errors.append(f"{candidate.path}: {result.error}")
                if stats.indexed == 1 or stats.indexed % 10 == 0 or stats.indexed == len(pending):
                    report()
                    connection.execute(
                        """
                        UPDATE index_runs SET
                            scanned=?, indexed=?, unchanged=?, failed=?, deleted=?, message=?
                        WHERE id=?
                        """,
                        (
                            stats.scanned,
                            stats.indexed,
                            stats.unchanged,
                            stats.failed,
                            stats.deleted,
                            f"処理中 ({stats.workers}並列): {stats.current_path}",
                            run_id,
                        ),
                    )
                    connection.commit()

            if pending and stats.workers == 1:
                for candidate in pending:
                    cancellation_point()
                    store(candidate, _prepare_candidate(candidate, config))
            elif pending:
                # Bound the queue because one prepared file may contain up to
                # max_text_chars_per_file characters plus its search terms.
                with multiprocessing.Manager() as manager:
                    cancel_signal = manager.Event()
                    try:
                        with ProcessPoolExecutor(max_workers=stats.workers) as executor:
                            iterator = iter(pending)
                            futures = {}
                            exhausted = False
                            while futures or not exhausted:
                                if should_cancel and should_cancel():
                                    cancel_signal.set()
                                    for future in futures:
                                        future.cancel()
                                    raise CancellationRequested()
                                while not exhausted and len(futures) < stats.workers * 2:
                                    try:
                                        candidate = next(iterator)
                                    except StopIteration:
                                        exhausted = True
                                        break
                                    future = executor.submit(
                                        _prepare_candidate,
                                        candidate,
                                        config,
                                        cancel_signal,
                                    )
                                    futures[future] = candidate
                                if not futures:
                                    continue
                                completed, _ = wait(
                                    futures,
                                    timeout=0.15,
                                    return_when=FIRST_COMPLETED,
                                )
                                for future in completed:
                                    candidate = futures.pop(future)
                                    store(candidate, future.result())
                    except CancellationRequested:
                        cancel_signal.set()
                        raise

            cancellation_point()
            if mode == "scan" and limit is None:
                stats.phase = "cleanup"
                report()
                active_rows = connection.execute(
                    "SELECT id, path FROM files WHERE is_deleted = 0 AND scope_hash=?",
                    (config.scope_hash,),
                ).fetchall()
                for row in active_rows:
                    cancellation_point()
                    normalized_path = os.path.normcase(os.path.normpath(row["path"]))
                    if normalized_path in seen_paths:
                        continue
                    with connection:
                        _delete_chunks(connection, row["id"])
                        connection.execute(
                            "UPDATE files SET is_deleted=1, indexed_at=? WHERE id=?",
                            (utc_now(), row["id"]),
                        )
                    stats.deleted += 1

                # Any rows left at this point referred to files that disappeared or
                # became excluded. The successful complete scan has superseded them.
                connection.execute(
                    "DELETE FROM index_queue WHERE scope_hash=?", (config.scope_hash,)
                )

                # A completed generation replaces older target/exclusion generations.
                stale_rows = connection.execute(
                    "SELECT id FROM files WHERE is_deleted=0 AND scope_hash<>?",
                    (config.scope_hash,),
                ).fetchall()
                for row in stale_rows:
                    cancellation_point()
                    with connection:
                        _delete_chunks(connection, row["id"])
                        connection.execute(
                            "UPDATE files SET is_deleted=1, indexed_at=? WHERE id=?",
                            (utc_now(), row["id"]),
                        )
                    stats.deleted += 1

            stats.phase = "finalizing"
            report()
            # FTS optimize rewrites the whole index. It is useful after a large update,
            # but disproportionately slow for ordinary runs with only a few changes.
            if stats.indexed + stats.deleted >= 100:
                connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            connection.execute(
                """
                UPDATE index_runs SET
                    finished_at=?, status='complete', scanned=?, indexed=?, unchanged=?,
                    failed=?, deleted=?, message=?
                WHERE id=?
                """,
                (
                    utc_now(),
                    stats.scanned,
                    stats.indexed,
                    stats.unchanged,
                    stats.failed,
                    stats.deleted,
                    "\n".join(stats.errors[:20]),
                    run_id,
                ),
            )
            connection.commit()
            stats.phase = "complete"
        except CancellationRequested:
            stats.cancelled = True
            stats.phase = "cancelled"
            connection.execute(
                """
                UPDATE index_runs SET
                    finished_at=?, status='cancelled', scanned=?, indexed=?, unchanged=?,
                    failed=?, deleted=?, message=?
                WHERE id=?
                """,
                (
                    utc_now(),
                    stats.scanned,
                    stats.indexed,
                    stats.unchanged,
                    stats.failed,
                    stats.deleted,
                    "ユーザー操作により安全に停止しました",
                    run_id,
                ),
            )
            connection.commit()
        except Exception as exc:
            connection.execute(
                "UPDATE index_runs SET finished_at=?, status='error', message=? WHERE id=?",
                (utc_now(), f"{type(exc).__name__}: {exc}", run_id),
            )
            connection.commit()
            raise

    stats.current_path = ""
    report()
    return stats
