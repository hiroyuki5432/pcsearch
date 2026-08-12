from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SearchConfig
from .extractors import extract_file
from .files import CandidateFile, iter_candidate_files


def _sample_evenly(items: list[CandidateFile], count: int) -> list[CandidateFile]:
    if count <= 0 or not items:
        return []
    ordered = sorted(items, key=lambda item: item.size)
    if len(ordered) <= count:
        return ordered
    if count == 1:
        return [ordered[len(ordered) // 2]]
    indices = {round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)}
    return [ordered[index] for index in sorted(indices)]


def _human_bytes(value: int | float) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024 or unit == "TB":
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{number:.2f} TB"


def build_inventory(config: SearchConfig, sample_per_type: int = 10) -> dict[str, Any]:
    candidates = list(iter_candidate_files(config))
    by_extension: dict[str, list[CandidateFile]] = defaultdict(list)
    for candidate in candidates:
        by_extension[candidate.extension].append(candidate)

    extension_reports: dict[str, Any] = {}
    estimated_text_bytes = 0
    sampled_total = 0
    sampled_failures = 0

    for extension, items in sorted(by_extension.items()):
        samples = _sample_evenly(
            [item for item in items if item.size <= config.max_file_size_bytes],
            sample_per_type,
        )
        ratios: list[float] = []
        sample_details: list[dict[str, Any]] = []
        for sample in samples:
            result = extract_file(sample.path, config)
            sampled_total += 1
            if result.status not in {"ok", "empty"}:
                sampled_failures += 1
            ratio = result.content_bytes / max(sample.size, 1)
            if result.status in {"ok", "empty"}:
                ratios.append(ratio)
            sample_details.append(
                {
                    "path": str(sample.path),
                    "source_bytes": sample.size,
                    "content_bytes": result.content_bytes,
                    "ratio": round(ratio, 6),
                    "status": result.status,
                    "error": result.error,
                }
            )

        source_bytes = sum(item.size for item in items)
        usable_ratios = [ratio for ratio in ratios if math.isfinite(ratio)]
        median_ratio = statistics.median(usable_ratios) if usable_ratios else 0.0
        extension_estimate = int(source_bytes * median_ratio)
        estimated_text_bytes += extension_estimate
        extension_reports[extension] = {
            "files": len(items),
            "source_bytes": source_bytes,
            "source_human": _human_bytes(source_bytes),
            "sample_count": len(samples),
            "median_text_ratio": round(median_ratio, 6),
            "estimated_text_bytes": extension_estimate,
            "estimated_text_human": _human_bytes(extension_estimate),
            "samples": sample_details,
        }

    metadata_budget = len(candidates) * 1_024
    estimate_low = int(estimated_text_bytes * 1.3 + metadata_budget)
    estimate_mid = int(estimated_text_bytes * 1.6 + metadata_budget)
    estimate_high = int(estimated_text_bytes * 2.0 + metadata_budget)
    source_total = sum(candidate.size for candidate in candidates)
    over_limit = sum(1 for candidate in candidates if candidate.size > config.max_file_size_bytes)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": [str(root) for root in config.roots],
        "eligible_files": len(candidates),
        "source_bytes": source_total,
        "source_human": _human_bytes(source_total),
        "files_over_size_limit": over_limit,
        "sampled_files": sampled_total,
        "sample_failures": sampled_failures,
        "estimated_text_bytes": estimated_text_bytes,
        "estimated_text_human": _human_bytes(estimated_text_bytes),
        "estimated_database": {
            "low_bytes": estimate_low,
            "mid_bytes": estimate_mid,
            "high_bytes": estimate_high,
            "low_human": _human_bytes(estimate_low),
            "mid_human": _human_bytes(estimate_mid),
            "high_human": _human_bytes(estimate_high),
        },
        "database_warning_bytes": config.database_warning_bytes,
        "database_warning_human": _human_bytes(config.database_warning_bytes),
        "extensions": extension_reports,
        "notes": [
            "推定値は形式・サイズごとの少数サンプルに基づきます。",
            "画像PDFや圧縮率の高いOffice文書では誤差が大きくなります。",
            "DB推定は抽出本文の1.3～2.0倍と、1ファイル1KBのメタデータ予算を加算しています。",
        ],
    }
    config.inventory_report_path.parent.mkdir(parents=True, exist_ok=True)
    config.inventory_report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report
