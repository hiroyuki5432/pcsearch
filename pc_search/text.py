from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from janome.tokenizer import Tokenizer


_SKIP_POS = {"助詞", "助動詞", "記号", "フィラー", "接続詞"}


@lru_cache(maxsize=1)
def _tokenizer() -> Tokenizer:
    return Tokenizer()


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def meaningful_tokens(value: str) -> list[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []
    tokens: list[str] = []
    for token in _tokenizer().tokenize(normalized):
        surface = token.surface.strip()
        major_pos = token.part_of_speech.split(",", 1)[0]
        if not surface or major_pos in _SKIP_POS:
            continue
        if all(unicodedata.category(char).startswith(("P", "Z", "C")) for char in surface):
            continue
        tokens.append(surface)
    return tokens


def terms_text(value: str) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    tokens = []
    for surface in _tokenizer().tokenize(normalized, wakati=True):
        surface = surface.strip()
        if not surface:
            continue
        if all(unicodedata.category(char).startswith(("P", "Z", "C")) for char in surface):
            continue
        tokens.append(surface)
    return " ".join(tokens)


def fts_query(value: str, mode: str = "and") -> tuple[str, list[str]]:
    tokens = list(dict.fromkeys(meaningful_tokens(value)))
    if not tokens:
        return "", []
    quoted = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
    operator = " OR " if mode.lower() == "or" else " AND "
    return operator.join(quoted), tokens


def make_snippet(content: str, query_tokens: list[str], radius: int = 180) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if not compact:
        return "本文を抽出できませんでした。"
    normalized = normalize_text(compact)
    position = -1
    for token in query_tokens:
        position = normalized.find(normalize_text(token))
        if position >= 0:
            break
    if position < 0:
        return compact[: radius * 2]
    start = max(0, position - radius)
    end = min(len(compact), position + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"
