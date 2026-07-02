"""Helpers to keep harness / Docker logs from filling the disk."""
from __future__ import annotations

_DIVE_PROGRESS_PREFIX = "[DIVE] progress:"
_RESULT_MARKERS = (
    "===DIVE_RESULT_BEGIN===",
    "===DIVE_RESULT_END===",
    "===COVERAGE_RESULT_BEGIN===",
    "===COVERAGE_RESULT_END===",
)


def compact_dive_output(text: str) -> str:
    """Drop repetitive DIVE progress lines, keeping only the last few."""
    if not text or _DIVE_PROGRESS_PREFIX not in text:
        return text

    kept: list[str] = []
    progress: list[str] = []
    for line in text.splitlines():
        if line.startswith(_DIVE_PROGRESS_PREFIX):
            progress.append(line)
            continue
        if progress:
            kept.extend(progress[-3:])
            progress = []
        kept.append(line)
    if progress:
        kept.extend(progress[-3:])
    return "\n".join(kept)


def truncate_log_text(text: str, max_bytes: int = 512_000, *, label: str = "output") -> str:
    """Cap log size while preserving harness result marker blocks when present."""
    if not text:
        return text

    text = compact_dive_output(text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text

    marker_chunks: list[str] = []
    for marker in _RESULT_MARKERS:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            end_marker = marker.replace("BEGIN", "END") if "BEGIN" in marker else None
            if end_marker and end_marker in text[idx:]:
                end = text.index(end_marker, idx) + len(end_marker)
                marker_chunks.append(text[idx:end])
                start = end
            else:
                marker_chunks.append(text[idx : idx + len(marker)])
                start = idx + len(marker)

    marker_blob = "\n\n".join(marker_chunks)
    marker_bytes = marker_blob.encode("utf-8", errors="replace")
    budget = max(8192, max_bytes - len(marker_bytes) - 256)
    if budget <= 0:
        return truncate_log_text(marker_blob, max_bytes=max_bytes, label=label)

    half = budget // 2
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    omitted = len(encoded) - half * 2
    return (
        f"{head}\n\n... [{label} truncated: omitted {omitted:,} bytes] ...\n\n"
        f"{marker_blob}\n\n"
        f"{tail}"
    )
