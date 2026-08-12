from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import socket
import threading
import urllib.error
import urllib.request
import webbrowser
from dataclasses import replace
from pathlib import Path

from pc_search.config import SearchConfig
from pc_search.database import initialize, search, status
from pc_search.indexer import run_index
from pc_search.inventory import build_inventory
from pc_search.web import serve
from pc_search.web import APP_VERSION


DEFAULT_CONFIG = Path(
    os.environ.get(
        "PC_FULLTEXT_SEARCH_CONFIG",
        str(Path(__file__).resolve().parent / "config.json"),
    )
)


def human_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{number:.2f} TB"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PC全文検索 v2")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config.json path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="容量を調査する")
    inventory_parser.add_argument("--sample-per-type", type=int, default=10)

    index_parser = subparsers.add_parser("index", help="差分索引を作成する")
    index_parser.add_argument("--limit", type=int)

    search_parser = subparsers.add_parser("search", help="コマンドラインで検索する")
    search_parser.add_argument("query")
    search_parser.add_argument("--mode", choices=("and", "or"), default="and")
    search_parser.add_argument("--extension", default="")
    search_parser.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("status", help="DB状態を表示する")
    serve_parser = subparsers.add_parser("serve", help="ローカル検索画面を起動する")
    serve_parser.add_argument("--port", type=int, help="一時的に待受ポートを変更する")
    serve_parser.add_argument("--open-browser", action="store_true", help="起動後にブラウザを開く")
    return parser


def _existing_server(host: str, port: int) -> dict | None:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            pass
    except OSError:
        return None
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=0.8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {"ok": False}


def main() -> None:
    args = make_parser().parse_args()
    config = SearchConfig.load(args.config)

    if args.command == "inventory":
        print("対象ファイルを調査しています。サンプル文書は読み取りますが変更しません。")
        report = build_inventory(config, sample_per_type=max(0, args.sample_per_type))
        estimate = report["estimated_database"]
        print(f"対象: {report['eligible_files']:,} files / {report['source_human']}")
        print(f"推定抽出本文: {report['estimated_text_human']}")
        print(
            "推定DB: "
            f"{estimate['low_human']} ～ {estimate['high_human']} "
            f"(中心 {estimate['mid_human']})"
        )
        print(f"レポート: {config.inventory_report_path}")
        return

    if args.command == "index":
        def progress(value):
            print(
                f"\r走査 {value['scanned']:,} / 更新 {value['indexed']:,} / "
                f"変更なし {value['unchanged']:,} / 失敗 {value['failed']:,}",
                end="",
                flush=True,
            )

        stats = run_index(config, limit=args.limit, progress=progress)
        print()
        print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
        print(f"DB: {config.database_path} ({human_bytes(config.database_path.stat().st_size)})")
        return

    if args.command == "search":
        if config.database_path.exists():
            initialize(config)
        results = search(
            config,
            args.query,
            mode=args.mode,
            extension=args.extension,
            limit=args.limit,
        )
        for index, result in enumerate(results, 1):
            print(f"[{index}] {result['filename']} — {result['location']}")
            print(f"    {result['path']}")
            print(f"    {result['snippet']}")
        print(f"\n{len(results)}件")
        return

    if args.command == "status":
        if config.database_path.exists():
            initialize(config)
        print(json.dumps(status(config), ensure_ascii=False, indent=2))
        return

    if args.command == "serve":
        if args.port is not None:
            config = replace(config, port=args.port)
        url = f"http://{config.host}:{config.port}"
        existing = _existing_server(config.host, config.port)
        if existing is not None:
            same_database = str(existing.get("database_path", "")).casefold() == str(
                config.database_path
            ).casefold()
            same_version = existing.get("version") == APP_VERSION
            if existing.get("ok") and same_database and same_version:
                print(f"PC全文検索は既に起動しています: {url}")
                if args.open_browser:
                    webbrowser.open(url)
                return
            raise SystemExit(
                f"ポート {config.port} は別の検索アプリが使用しています。\n"
                f"既存: {existing.get('config_path', '識別できません')}\n"
                f"既存バージョン: {existing.get('version', '不明')} / 今回: {APP_VERSION}\n"
                f"今回: {config.config_path}\n"
                "古いプロセスを確認するか、--portで別ポートを指定してください。"
            )
        print(f"PC全文検索: {url}")
        if args.open_browser:
            threading.Timer(0.8, webbrowser.open, args=(url,)).start()
        serve(config)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
