from __future__ import annotations

from dataclasses import dataclass, field


class CancellationRequested(Exception):
    """Raised for a cooperative, safe indexing cancellation."""


@dataclass
class TextChunk:
    location: str
    content: str


@dataclass
class ExtractionResult:
    chunks: list[TextChunk] = field(default_factory=list)
    status: str = "ok"
    error: str = ""
    truncated: bool = False

    @property
    def content_chars(self) -> int:
        return sum(len(chunk.content) for chunk in self.chunks)

    @property
    def content_bytes(self) -> int:
        return sum(len(chunk.content.encode("utf-8")) for chunk in self.chunks)


@dataclass
class PreparedIndexFile:
    result: ExtractionResult
    content_terms: list[str]
    filename_terms: str
    path_terms: str
    content_hash: str = ""
