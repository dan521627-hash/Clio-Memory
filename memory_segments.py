"""Lossless views over timestamp-separated memory bucket content."""

from __future__ import annotations

import re


TIMESTAMP_SEGMENT_RE = re.compile(
    r"(?m)^--- (?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}) ---\r?$"
)
CONTENT_DATE_RE = re.compile(r"【(?P<date>\d{4}-\d{2}-\d{2})】")


def segment_timestamp(content: str, fallback: str) -> str:
    """Prefer an explicit content date; otherwise use the write timestamp."""
    match = CONTENT_DATE_RE.search(str(content or ""))
    return f"{match.group('date')}T00:00" if match else str(fallback)[:16]


def segment_marker(timestamp: str) -> str:
    return f"--- {str(timestamp)[:16]} ---"


def append_memory_segment(source: str, fragment: str, timestamp: str) -> str:
    old_text = str(source or "")
    new_text = str(fragment or "")
    separator = "\n" if old_text.endswith("\n") else "\n\n"
    return f"{old_text}{separator}{segment_marker(timestamp)}\n{new_text}"


def prepend_memory_segment(
    source: str,
    fragment: str,
    timestamp: str,
    created_at: str = "",
) -> str:
    """Insert one older packet before the original first packet."""
    old_text = str(source or "")
    new_text = str(fragment or "")
    first_marker = TIMESTAMP_SEGMENT_RE.match(old_text)
    prefix = f"{segment_marker(timestamp)}\n{new_text}\n\n"
    if first_marker:
        return prefix + old_text
    original_timestamp = str(created_at or timestamp)[:16]
    return prefix + f"{segment_marker(original_timestamp)}\n{old_text}"


def package_single_insertion(
    old_content: str,
    edited_content: str,
    timestamp: str,
    created_at: str = "",
) -> tuple[str, str]:
    """Turn one pure insertion at a packet boundary into a timestamped packet."""
    old_text = str(old_content or "")
    edited_text = str(edited_content or "")
    if len(edited_text) <= len(old_text):
        return edited_text, ""

    prefix_length = 0
    prefix_limit = min(len(old_text), len(edited_text))
    while (
        prefix_length < prefix_limit
        and old_text[prefix_length] == edited_text[prefix_length]
    ):
        prefix_length += 1

    suffix_length = 0
    while (
        suffix_length < len(old_text) - prefix_length
        and old_text[-(suffix_length + 1)] == edited_text[-(suffix_length + 1)]
    ):
        suffix_length += 1

    inserted_end = len(edited_text) - suffix_length if suffix_length else len(edited_text)
    inserted = edited_text[prefix_length:inserted_end]
    edited_tail = edited_text[inserted_end:]
    if edited_text[:prefix_length] + edited_tail != old_text or not inserted.strip():
        return edited_text, ""

    packet_timestamp = segment_timestamp(inserted, timestamp)
    if prefix_length == len(old_text):
        return append_memory_segment(old_text, inserted.strip(), packet_timestamp), "append"
    if prefix_length == 0:
        return (
            prepend_memory_segment(
                old_text, inserted.strip(), packet_timestamp, created_at
            ),
            "prepend",
        )

    boundaries = [0, *(match.start() for match in TIMESTAMP_SEGMENT_RE.finditer(old_text)), len(old_text)]
    boundary = min(boundaries, key=lambda value: (abs(value - prefix_length), value))
    if boundary == 0:
        return (
            prepend_memory_segment(
                old_text, inserted.strip(), packet_timestamp, created_at
            ),
            "prepend",
        )
    if boundary == len(old_text):
        return append_memory_segment(old_text, inserted.strip(), packet_timestamp), "append"

    before = old_text[:boundary]
    after = old_text[boundary:]
    leading_separator = "\n" if before.endswith("\n") else "\n\n"
    trailing_separator = "" if after.startswith(("\n", "\r")) else "\n\n"
    packaged = (
        f"{before}{leading_separator}{segment_marker(packet_timestamp)}\n"
        f"{inserted.strip()}{trailing_separator}{after}"
    )
    return packaged, "insert"


def _without_marker(raw_text: str, marker_end: int = 0) -> str:
    content = raw_text[marker_end:]
    if content.startswith("\r\n"):
        return content[2:]
    if content.startswith("\n"):
        return content[1:]
    return content


def split_memory_segments(content: str, created_at: str = "") -> list[dict]:
    """Return chronological, lossless packet views without rewriting source."""
    source = str(content or "")
    matches = list(TIMESTAMP_SEGMENT_RE.finditer(source))
    created_label = str(created_at or "").strip()[:16]
    if not matches:
        return [
            {
                "segment_id": "p0001",
                "source_index": 1,
                "timestamp": created_label,
                "raw_text": source,
                "content": source,
                "is_initial": True,
            }
        ]

    segments: list[dict] = []
    if matches[0].start() > 0:
        initial = source[: matches[0].start()]
        segments.append(
            {
                "segment_id": "p0001",
                "source_index": 1,
                "timestamp": created_label,
                "raw_text": initial,
                "content": initial,
                "is_initial": True,
            }
        )

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        raw_text = source[match.start() : end]
        marker_length = match.end() - match.start()
        source_index = len(segments) + 1
        segments.append(
            {
                "segment_id": f"p{source_index:04d}",
                "source_index": source_index,
                "timestamp": match.group("timestamp"),
                "raw_text": raw_text,
                "content": _without_marker(raw_text, marker_length),
                "is_initial": False,
            }
        )

    return segments or [
        {
            "segment_id": "p0001",
            "source_index": 1,
            "timestamp": created_label,
            "raw_text": source,
            "content": source,
            "is_initial": True,
        }
    ]


def latest_memory_segment(content: str, created_at: str = "") -> dict:
    return split_memory_segments(content, created_at)[-1]
