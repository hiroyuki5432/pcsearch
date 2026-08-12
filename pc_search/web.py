from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from .config import SearchConfig, update_config
from .database import (
    duplicate_paths,
    exclusion_suggestions,
    extraction_issues,
    file_hits,
    indexed_path_for_file_id,
    initialize,
    path_impact,
    search_page,
    status,
    storage_breakdown,
)
from .indexer import run_index
from .inventory import build_inventory


APP_VERSION = "2.3.0"


def _date_ns(value: str, *, end: bool = False) -> int:
    if not value:
        return 0
    parsed = datetime.fromisoformat(value)
    if end and len(value) <= 10:
        parsed += timedelta(days=1)
    return int(parsed.timestamp() * 1_000_000_000)


def create_app(config: SearchConfig) -> Flask:
    template_folder = Path(__file__).resolve().parent.parent / "templates"
    app = Flask(__name__, template_folder=str(template_folder))
    initialize(config)

    config_lock = threading.Lock()
    config_state: dict[str, Any] = {
        "value": config,
        "mtime": config.config_path.stat().st_mtime_ns,
    }

    def current_config() -> SearchConfig:
        with config_lock:
            try:
                mtime = config.config_path.stat().st_mtime_ns
                if mtime != config_state["mtime"]:
                    loaded = SearchConfig.load(config.config_path)
                    initialize(loaded)
                    config_state.update({"value": loaded, "mtime": mtime})
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            return config_state["value"]

    def replace_config(changes: dict[str, Any]) -> SearchConfig:
        if index_state["running"]:
            raise RuntimeError("索引更新中は設定を変更できません")
        with config_lock:
            loaded = update_config(config.config_path, changes)
            initialize(loaded)
            config_state.update(
                {"value": loaded, "mtime": loaded.config_path.stat().st_mtime_ns}
            )
            return loaded

    index_lock = threading.Lock()
    cancel_event = threading.Event()
    index_state: dict[str, Any] = {
        "running": False,
        "cancel_requested": False,
        "error": "",
        "progress": {},
    }
    inventory_lock = threading.Lock()
    inventory_state: dict[str, Any] = {"running": False, "error": ""}

    @app.errorhandler(RuntimeError)
    @app.errorhandler(ValueError)
    def expected_error(error):
        return jsonify({"error": str(error)}), 409 if isinstance(error, RuntimeError) else 400

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/api/health")
    def api_health():
        active = current_config()
        return jsonify(
            {
                "ok": True,
                "version": APP_VERSION,
                "config_path": str(active.config_path),
                "database_path": str(active.database_path),
                "scope_hash": active.scope_hash,
            }
        )

    @app.get("/api/status")
    def api_status():
        active = current_config()
        payload = status(active)
        payload["indexing"] = dict(index_state)
        payload["inventory_running"] = inventory_state["running"]
        payload["inventory_error"] = inventory_state["error"]
        payload["warning_bytes"] = active.database_warning_bytes
        payload["roots"] = [str(root) for root in active.roots]
        payload["config_path"] = str(active.config_path)
        payload["database_path"] = str(active.database_path)
        payload["version"] = APP_VERSION
        payload["auto_index_interval_minutes"] = active.auto_index_interval_minutes
        payload["inventory"] = None
        payload["inventory_mismatch"] = False
        if active.inventory_report_path.exists():
            try:
                inventory = json.loads(active.inventory_report_path.read_text(encoding="utf-8"))
                report_roots = {str(Path(value)).casefold() for value in inventory.get("roots", [])}
                active_roots = {str(value).casefold() for value in active.roots}
                if report_roots == active_roots:
                    payload["inventory"] = inventory
                else:
                    payload["inventory_mismatch"] = True
            except (OSError, json.JSONDecodeError):
                pass
        return jsonify(payload)

    @app.get("/api/search")
    def api_search():
        active = current_config()
        query = request.args.get("q", "").strip()
        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        if not query:
            return jsonify({"query": query, "results": [], "count": 0, "has_more": False})
        extension = request.args.get("extension", "")
        extension = {
            "excel": "xlsx,xls",
            "word": "docx",
            "powerpoint": "pptx",
        }.get(extension, extension)
        statuses = tuple(filter(None, request.args.get("statuses", "").split(",")))
        page = search_page(
            active,
            query,
            mode=request.args.get("mode", "and"),
            extension=extension,
            limit=limit,
            offset=offset,
            folder=request.args.get("folder", "").strip(),
            statuses=statuses,
            content_only=request.args.get("content_only", "0") == "1",
            min_size=request.args.get("min_size", 0, type=int),
            max_size=request.args.get("max_size", 0, type=int),
            modified_after_ns=_date_ns(request.args.get("date_from", "")),
            modified_before_ns=_date_ns(request.args.get("date_to", ""), end=True),
            sort=request.args.get("sort", "relevance"),
        )
        return jsonify({"query": query, "count": len(page["results"]), **page})

    @app.get("/api/file-hits/<int:file_id>")
    def api_file_hits(file_id: int):
        return jsonify(
            {
                "hits": file_hits(
                    current_config(),
                    file_id,
                    request.args.get("q", ""),
                    request.args.get("mode", "and"),
                )
            }
        )

    @app.get("/api/duplicates/<int:file_id>")
    def api_duplicates(file_id: int):
        return jsonify({"files": duplicate_paths(current_config(), file_id)})

    @app.get("/api/issues")
    def api_issues():
        return jsonify(
            {
                "items": extraction_issues(
                    current_config(),
                    status_filter=request.args.get("status", ""),
                    limit=request.args.get("limit", 200, type=int),
                )
            }
        )

    @app.get("/api/storage")
    def api_storage():
        return jsonify(
            storage_breakdown(
                current_config(),
                limit=request.args.get("limit", 15, type=int),
                parent=request.args.get("parent", ""),
            )
        )

    @app.get("/api/settings")
    def api_settings():
        active = current_config()
        return jsonify(
            {
                "roots": [str(value) for value in active.roots],
                "extensions": sorted(active.extensions),
                "exclude_folder_names": sorted(active.exclude_folder_names),
                "exclude_folder_globs": list(active.exclude_folder_globs),
                "exclude_file_globs": list(active.exclude_file_globs),
                "exclude_folder_paths": [str(value) for value in active.exclude_folder_paths],
                "exclude_file_paths": [str(value) for value in active.exclude_file_paths],
                "file_policies": active.file_policies,
                "table_head_rows": active.table_head_rows,
                "max_file_size_mb": active.max_file_size_bytes // (1024 * 1024),
                "max_text_chars_per_file": active.max_text_chars_per_file,
                "index_workers": active.index_workers,
                "auto_index_interval_minutes": active.auto_index_interval_minutes,
                "config_path": str(active.config_path),
                "database_path": str(active.database_path),
                "scope_hash": active.scope_hash,
            }
        )

    @app.post("/api/settings")
    def api_settings_update():
        data = request.get_json(silent=True) or {}
        allowed = {
            "roots", "exclude_folder_names", "exclude_folder_globs", "exclude_file_globs",
            "exclude_folder_paths", "exclude_file_paths", "file_policies", "table_head_rows",
            "max_file_size_mb", "max_text_chars_per_file", "index_workers",
            "auto_index_interval_minutes",
        }
        changes = {key: value for key, value in data.items() if key in allowed}
        if "roots" in changes and not changes["roots"]:
            raise ValueError("検索対象フォルダを1つ以上指定してください")
        before = current_config().scope_hash
        active = replace_config(changes)
        return jsonify({"saved": True, "scope_changed": before != active.scope_hash})

    @app.get("/api/exclusion-suggestions")
    def api_exclusion_suggestions():
        return jsonify({"items": exclusion_suggestions(current_config())})

    @app.get("/api/path-impact")
    def api_path_impact():
        path = request.args.get("path", "").strip()
        if not path:
            raise ValueError("pathが必要です")
        return jsonify(
            path_impact(
                current_config(), path, is_file=request.args.get("kind") == "file"
            )
        )

    @app.post("/api/exclusions")
    def api_exclusions():
        data = request.get_json(silent=True) or {}
        path = str(data.get("path", "")).strip()
        kind = data.get("kind", "folder")
        action = data.get("action", "add")
        if not path or kind not in {"folder", "file"} or action not in {"add", "remove"}:
            raise ValueError("除外対象・種類・操作を確認してください")
        active = current_config()
        key = "exclude_folder_paths" if kind == "folder" else "exclude_file_paths"
        values = [str(value) for value in getattr(active, key)]
        normalized = str(Path(path).resolve())
        if action == "add" and normalized not in values:
            values.append(normalized)
        if action == "remove":
            values = [value for value in values if value.casefold() != normalized.casefold()]
        updated = replace_config({key: values})
        return jsonify({"saved": True, "scope_hash": updated.scope_hash})

    @app.post("/api/file-policy")
    def api_file_policy():
        data = request.get_json(silent=True) or {}
        path = str(data.get("path", "")).strip()
        policy = str(data.get("policy", "")).lower()
        if not path or policy not in {"full", "head", "metadata", "exclude", "default"}:
            raise ValueError("ファイルと処理方法を確認してください")
        active = current_config()
        policies = dict(active.file_policies)
        normalized = str(Path(path).resolve())
        if policy == "default":
            policies.pop(normalized, None)
        else:
            policies[normalized] = policy
        updated = replace_config({"file_policies": policies})
        return jsonify({"saved": True, "scope_hash": updated.scope_hash})

    def background_inventory(active: SearchConfig) -> None:
        try:
            build_inventory(active)
        except Exception as exc:
            inventory_state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            inventory_state["running"] = False
            inventory_lock.release()

    @app.post("/api/inventory")
    def api_inventory():
        if not inventory_lock.acquire(blocking=False):
            return jsonify({"started": False, "message": "容量見積は実行中です"}), 409
        active = current_config()
        inventory_state.update({"running": True, "error": ""})
        threading.Thread(
            target=background_inventory, args=(active,), name="pc-search-inventory", daemon=True
        ).start()
        return jsonify({"started": True})

    def background_index(active: SearchConfig) -> None:
        try:
            stats = run_index(
                active,
                progress=lambda value: index_state.update({"progress": value}),
                should_cancel=cancel_event.is_set,
            )
            index_state["progress"] = stats.as_dict()
        except Exception as exc:
            index_state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            index_state["running"] = False
            index_lock.release()

    def auto_index_scheduler() -> None:
        while True:
            time.sleep(30)
            active = current_config()
            interval = active.auto_index_interval_minutes
            if interval <= 0 or index_state["running"]:
                continue
            current = status(active)
            finished = (current.get("last_run") or {}).get("finished_at")
            if finished:
                try:
                    age = datetime.now().astimezone() - datetime.fromisoformat(finished).astimezone()
                    if age.total_seconds() < interval * 60:
                        continue
                except ValueError:
                    pass
            if not index_lock.acquire(blocking=False):
                continue
            cancel_event.clear()
            index_state.update(
                {"running": True, "cancel_requested": False, "error": "", "progress": {}}
            )
            threading.Thread(
                target=background_index,
                args=(active,),
                name="pc-search-auto-index",
                daemon=True,
            ).start()

    threading.Thread(
        target=auto_index_scheduler, name="pc-search-scheduler", daemon=True
    ).start()

    @app.post("/api/index")
    def api_index():
        if not index_lock.acquire(blocking=False):
            return jsonify({"started": False, "message": "索引更新は実行中です"}), 409
        cancel_event.clear()
        index_state.update(
            {"running": True, "cancel_requested": False, "error": "", "progress": {}}
        )
        active = current_config()
        threading.Thread(
            target=background_index, args=(active,), name="pc-search-indexer", daemon=True
        ).start()
        return jsonify({"started": True})

    @app.post("/api/index/cancel")
    def api_index_cancel():
        if not index_state["running"]:
            return jsonify({"cancel_requested": False, "message": "索引更新は実行されていません"}), 409
        cancel_event.set()
        index_state["cancel_requested"] = True
        return jsonify({"cancel_requested": True})

    def valid_indexed_path() -> tuple[Path | None, Any]:
        data = request.get_json(silent=True) or {}
        file_id = data.get("file_id")
        if not isinstance(file_id, int):
            return None, (jsonify({"error": "file_id is required"}), 400)
        path = indexed_path_for_file_id(current_config(), file_id)
        if path is None or not path.is_file():
            return None, (jsonify({"error": "元ファイルが見つかりません"}), 404)
        return path, None

    @app.post("/api/open-file")
    def open_file():
        path, error = valid_indexed_path()
        if error:
            return error
        assert path is not None
        os.startfile(path)  # type: ignore[attr-defined]
        return jsonify({"opened": True})

    @app.post("/api/show-in-folder")
    def show_in_folder():
        path, error = valid_indexed_path()
        if error:
            return error
        assert path is not None
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return jsonify({"opened": True})

    return app


def serve(config: SearchConfig) -> None:
    app = create_app(config)
    app.run(host=config.host, port=config.port, debug=False, use_reloader=False)
