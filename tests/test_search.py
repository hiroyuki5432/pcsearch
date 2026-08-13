from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from pc_search.config import SearchConfig, update_config
from pc_search.database import duplicate_paths, file_hits, search, search_page, status, storage_breakdown
from pc_search.indexer import run_index
from pc_search.inventory import build_inventory
from pc_search.web import create_app


def make_config(tmp_path: Path) -> SearchConfig:
    root = tmp_path / "documents"
    root.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "database_path": "data/index.db",
                "inventory_report_path": "data/inventory.json",
                "roots": [str(root)],
                "extensions": [".txt", ".csv"],
                "exclude_folder_names": ["venv"],
                "exclude_folder_globs": ["*_bk"],
                "exclude_file_globs": ["~$*"],
                "max_file_size_mb": 5,
                "chunk_chars": 500,
                "chunk_overlap_chars": 50,
                "index_workers": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return SearchConfig.load(config_path)


class SearchTests(unittest.TestCase):
    def test_inventory_and_japanese_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            root = config.roots[0]
            (root / "修繕メモ.txt").write_text(
                "203号室の給湯器を交換した。費用は148500円。",
                encoding="utf-8",
            )
            (root / "契約.csv").write_text(
                "物件,内容\n物件A,管理委託契約を更新\n",
                encoding="utf-8",
            )
            excluded = root / "venv"
            excluded.mkdir()
            (excluded / "noise.txt").write_text("検索されない給湯器", encoding="utf-8")
            backup = root / "documents_bk"
            backup.mkdir()
            (backup / "backup.txt").write_text("検索されない契約", encoding="utf-8")

            report = build_inventory(config, sample_per_type=2)
            self.assertEqual(report["eligible_files"], 2)
            self.assertGreater(report["estimated_database"]["mid_bytes"], 0)

            first = run_index(config)
            self.assertEqual(first.indexed, 2)
            self.assertEqual(first.failed, 0)

            results = search(config, "給湯器")
            self.assertTrue(results)
            self.assertEqual(results[0]["filename"], "修繕メモ.txt")
            self.assertIn("給湯器", results[0]["snippet"])

            contract_results = search(config, "管理 契約")
            self.assertTrue(contract_results)
            self.assertEqual(contract_results[0]["filename"], "契約.csv")

            current = status(config)
            self.assertEqual(current["files"], 2)
            self.assertEqual(current["chunks"], 2)

            storage = storage_breakdown(config)
            self.assertEqual(storage["files"], 2)
            self.assertEqual(
                {item["name"] for item in storage["top_files"]},
                {"修繕メモ.txt", "契約.csv"},
            )
            self.assertGreater(storage["content_bytes"], 0)

            app = create_app(config)
            client = app.test_client()
            self.assertEqual(client.get("/").status_code, 200)
            api_response = client.get("/api/search", query_string={"q": "給湯器"})
            self.assertEqual(api_response.status_code, 200)
            self.assertEqual(api_response.get_json()["count"], 1)
            storage_response = client.get("/api/storage")
            self.assertEqual(storage_response.status_code, 200)
            self.assertEqual(storage_response.get_json()["files"], 2)

    def test_incremental_update_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            path = config.roots[0] / "note.txt"
            path.write_text("古い設備情報", encoding="utf-8")

            run_index(config)
            unchanged = run_index(config)
            self.assertEqual(unchanged.unchanged, 1)

            time.sleep(0.01)
            path.write_text("新しい給湯器情報", encoding="utf-8")
            updated = run_index(config)
            self.assertEqual(updated.indexed, 1)
            self.assertTrue(search(config, "給湯器"))
            self.assertFalse(search(config, "古い"))

            path.unlink()
            deleted = run_index(config)
            self.assertEqual(deleted.deleted, 1)
            self.assertFalse(search(config, "給湯器"))

    def test_parallel_index_and_safe_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(make_config(Path(directory)), index_workers=2)
            for index in range(4):
                (config.roots[0] / f"文書{index}.txt").write_text(
                    f"並列索引の確認文書 {index}", encoding="utf-8"
                )

            parallel = run_index(config)
            self.assertEqual(parallel.indexed, 4)
            self.assertEqual(parallel.workers, 2)
            self.assertTrue(search(config, "並列 索引"))

            (config.roots[0] / "Web停止確認.txt").write_text(
                "停止対象 " * 50_000, encoding="utf-8"
            )
            client = create_app(config).test_client()
            self.assertEqual(client.post("/api/index").status_code, 200)
            self.assertEqual(client.post("/api/index/cancel").status_code, 200)
            deadline = time.monotonic() + 10
            indexing = {}
            while time.monotonic() < deadline:
                indexing = client.get("/api/status").get_json()["indexing"]
                if not indexing["running"]:
                    break
                time.sleep(0.05)
            self.assertFalse(indexing["running"])
            self.assertTrue(indexing["progress"]["cancelled"])

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            (config.roots[0] / "停止確認.txt").write_text("安全停止", encoding="utf-8")
            cancelled = run_index(config, should_cancel=lambda: True)
            self.assertTrue(cancelled.cancelled)
            self.assertEqual(status(config)["last_run"]["status"], "cancelled")

    def test_resume_uses_persisted_queue_without_full_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            for index in range(3):
                (config.roots[0] / f"再開{index}.txt").write_text(
                    f"再開対象の本文 {index}", encoding="utf-8"
                )

            cancel = {"requested": False}

            def stop_during_scan(value):
                if value["phase"] == "scanning" and value["scanned"] == 1:
                    cancel["requested"] = True

            interrupted = run_index(
                config,
                progress=stop_during_scan,
                should_cancel=lambda: cancel["requested"],
            )
            self.assertTrue(interrupted.cancelled)
            self.assertEqual(status(config)["resume_pending"], 1)

            # This file was created after the interrupted scan, so resume must not
            # discover it. A later complete scan will pick it up.
            (config.roots[0] / "走査後.txt").write_text("後から追加", encoding="utf-8")
            resumed = run_index(config, mode="resume")
            self.assertEqual(resumed.indexed, 1)
            self.assertEqual(resumed.scanned, 1)
            self.assertEqual(status(config)["resume_pending"], 0)
            self.assertFalse(search(config, "後から追加"))

            complete = run_index(config)
            self.assertEqual(complete.indexed, 3)
            self.assertTrue(search(config, "後から追加"))

    def test_content_ranking_duplicates_filters_and_management_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            root = config.roots[0]
            (root / "契約という名前だけ.txt").write_text("", encoding="utf-8")
            body = "管理会社との契約を2026年に更新する"
            first = root / "本文1.txt"
            second = root / "本文2.txt"
            first.write_text(body, encoding="utf-8")
            second.write_text(body, encoding="utf-8")
            run_index(config)

            page = search_page(config, "契約", limit=2)
            self.assertEqual(len(page["results"]), 2)
            self.assertFalse(page["has_more"])
            self.assertEqual(page["results"][0]["match_kind"], "content")
            self.assertGreaterEqual(page["results"][0]["duplicate_count"], 2)
            self.assertEqual(len(file_hits(config, page["results"][0]["file_id"], "契約")), 1)
            self.assertEqual(len(duplicate_paths(config, page["results"][0]["file_id"])), 2)

            content_only = search_page(config, "契約", content_only=True)
            self.assertTrue(all(item["match_kind"] == "content" for item in content_only["results"]))

            app = create_app(config)
            client = app.test_client()
            settings = client.get("/api/settings").get_json()
            self.assertEqual(settings["database_path"], str(config.database_path))
            suggestions = client.get("/api/exclusion-suggestions")
            self.assertEqual(suggestions.status_code, 200)
            impact = client.get("/api/path-impact", query_string={"path": str(root)}).get_json()
            self.assertEqual(impact["files"], 3)

            response = client.post(
                "/api/file-policy",
                json={"path": str(first), "policy": "metadata"},
            )
            self.assertEqual(response.status_code, 200)
            updated = SearchConfig.load(config.config_path)
            self.assertEqual(status(updated)["other_scope_files"], 0)
            self.assertEqual(run_index(updated).indexed, 1)

    def test_scope_change_reuses_unchanged_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            keep = config.roots[0] / "keep"
            excluded = config.roots[0] / "exclude-later"
            keep.mkdir()
            excluded.mkdir()
            (keep / "keep.txt").write_text("残す契約", encoding="utf-8")
            (excluded / "remove.txt").write_text("除外する契約", encoding="utf-8")
            first = run_index(config)
            self.assertEqual(first.indexed, 2)

            changed = update_config(
                config.config_path,
                {"exclude_folder_paths": [str(excluded)]},
            )
            second = run_index(changed)
            self.assertEqual(second.indexed, 0)
            self.assertEqual(second.unchanged, 1)
            self.assertGreaterEqual(second.deleted, 1)
            self.assertEqual([item["filename"] for item in search(changed, "契約")], ["keep.txt"])

            policy_config = update_config(
                changed.config_path,
                {"file_policies": {str(keep / "keep.txt"): "metadata"}},
            )
            policy_run = run_index(policy_config)
            self.assertEqual(policy_run.indexed, 1)


if __name__ == "__main__":
    unittest.main()
