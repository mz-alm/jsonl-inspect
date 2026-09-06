"""JSONL loading and analysis for Claude Code session files.

Pre-computes per-record sizes, type breakdowns, composition stats, and
parent-chain validation. Designed to be loaded once at server startup and
held in memory for the lifetime of the inspector session.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# Record types whose contents are sent to Anthropic's API on each request.
# Lesson from JSONL surgery work: editing these affects the wire payload;
# editing other types is purely local metadata.
API_RELEVANT_TYPES = {"user", "assistant", "attachment"}

# Content block types that appear inside user/assistant message.content lists.
BLOCK_TYPES = {"text", "thinking", "tool_use", "tool_result", "image"}

PREVIEW_CHARS = 200


@dataclass
class BlockSummary:
    """Per-block analysis for a content block inside a user/assistant message."""

    index: int            # position within the message.content array
    type: str             # text / thinking / tool_use / tool_result / image / unknown
    size: int             # serialized JSON size of the block in bytes
    preview: str          # short human-readable summary
    # tool_use-specific
    tool_name: str | None = None
    tool_use_id: str | None = None
    # tool_result-specific
    tool_result_for: str | None = None  # the tool_use_id this result responds to
    is_trimmed: bool = False  # heuristic: starts with "[trimmed"


@dataclass
class RecordSummary:
    """Per-record summary for the inspector frontend.

    Holds analytical metadata + a preview, NOT the full raw record. Full raw
    record is fetched on demand via Session.get_record(idx).
    """

    index: int
    uuid: str | None
    parent_uuid: str | None
    type: str
    subtype: str | None
    timestamp: str | None
    size: int
    is_api_relevant: bool
    preview: str
    blocks: list[BlockSummary] = field(default_factory=list)
    # True iff this record is reachable from the active tip by walking parentUuid
    # backward.
    is_chain_reachable: bool = True
    # True iff this record's index is before the latest compact_boundary's index.
    # Positional fact, not a state — applies regardless of pluck status.
    is_pre_compaction: bool = False
    # True iff this record was explicitly unlinked by a user pluck operation
    # (detected via the "_inspector_plucked" marker field in the record).
    is_plucked: bool = False
    # True iff this record has any field-level mutations recorded.
    # Mutations are tracked in the "_inspector_mutations" dict, which stores
    # original values keyed by dotted field path so changes are reversible.
    is_mutated: bool = False
    # List of mutated field paths (e.g. "message.content"). Empty if not mutated.
    mutated_fields: list[str] = field(default_factory=list)

    # Why this record's content contributes to the wire payload, if at all:
    #   - "chain": record is on the parentUuid chain walk from the tip (chain walk)
    #   - "sibling_merge": off-chain assistant pulled in via message.id match
    #     with an in-chain assistant (message.id sibling merge)
    #   - "parent_reattach": off-chain user with tool_result content whose
    #     parentUuid matches an in-chain uuid (tool_result parent-reattach)
    #   - None: not on the wire (orphan)
    wire_inclusion_reason: str | None = None
    # For sibling-merged records: how many OTHER records share this message.id
    # (i.e., size of the merge group minus self). 0 if not part of a merge.
    sibling_count: int = 0


@dataclass
class SessionStats:
    """Whole-file analytical summary."""

    total_records: int
    total_bytes: int
    api_bytes: int

    # Wire-payload view: records that contribute content to the API messages
    # array on the next request, after simulating Claude Code's the wire-assembly step
    # pipeline (chain walk + message.id sibling merge + tool_result reattach).
    # Bytes count only the `{role, content}` fields that actually go on the
    # wire (local-only fields like uuid/parentUuid/_inspector_* are excluded).
    # This is what determines context cost, not disk presence.
    wire_messages_count: int
    wire_messages_bytes: int
    # API-relevant records that don't contribute to the wire payload (i.e.,
    # not chain-reachable AND not reattached by message.id or parentUuid).
    orphan_record_indices: list[int]

    # Counts
    type_counts: dict[str, int]      # record-type -> count
    block_type_counts: dict[str, int]  # block-type -> count

    # Bytes
    bytes_by_type: dict[str, int]         # record-type -> total bytes
    bytes_by_block_type: dict[str, int]   # block-type -> total bytes (API only)

    # Rankings (record_index, size)
    top_records_by_size: list[tuple[int, int]]
    # (record_index, block_index, block_type, size)
    top_blocks_by_size: list[tuple[int, int, str, int]]

    # Health checks
    orphan_parent_uuids: list[tuple[int, str]]  # (record_idx, missing_parent_uuid)
    tool_use_without_result: list[str]          # tool_use_ids
    tool_result_without_use: list[str]          # tool_use_ids referenced but absent

    # Compaction events: each system/compact_boundary record's metadata
    compaction_events: list[dict[str, Any]] = field(default_factory=list)

    # Pending-changes view: how many staged operations are waiting for /save.
    # Frontend uses this to enable the Save/Undo buttons and warn on unload.
    pending_ops_count: int = 0

    # Cached preflight signal: the latest in-chain assistant's
    # `message.usage.input_tokens`. This is what Claude Code's preflight
    # check reads to estimate context fill BEFORE firing a new request,
    # and what the statusline displays. Compare against our estimated
    # wire bytes / tokens to detect a stale-cache disconnect (which is
    # what `refresh_preflight_usage` is designed to fix).
    latest_input_tokens: int | None = None
    latest_input_tokens_idx: int | None = None
    latest_cache_creation_tokens: int | None = None
    latest_cache_read_tokens: int | None = None


# ---------------------------------------------------------------------------
# Block & record analysis


def _truncate(s: str, n: int = PREVIEW_CHARS) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def _block_preview(block: dict[str, Any]) -> str:
    """Short human-readable preview of a content block."""
    btype = block.get("type")
    if btype == "text":
        return _truncate(block.get("text", ""))
    if btype == "thinking":
        text = block.get("thinking", "")
        sig_len = len(block.get("signature", ""))
        if text:
            return _truncate(text)
        if sig_len:
            return f"<thinking text redacted, {sig_len}b signature>"
        return "<thinking, empty>"
    if btype == "tool_use":
        name = block.get("name", "?")
        inp = block.get("input", {})
        # Try to surface the most informative input field
        for key in ("command", "file_path", "query", "pattern", "url", "path"):
            if isinstance(inp, dict) and key in inp:
                val = inp[key]
                if isinstance(val, str):
                    return f"{name}({key}={_truncate(val, 120)})"
        return f"{name}(…)"
    if btype == "tool_result":
        content = block.get("content", "")
        if isinstance(content, list):
            for sub in content:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    return _truncate(sub.get("text", ""))
            return f"<tool_result, {len(content)} sub-blocks>"
        return _truncate(str(content))
    if btype == "image":
        source = block.get("source", {})
        media_type = source.get("media_type", "?") if isinstance(source, dict) else "?"
        return f"<image, {media_type}>"
    return f"<{btype or 'unknown'}>"


def _analyze_block(idx: int, block: dict[str, Any]) -> BlockSummary:
    size = len(json.dumps(block, ensure_ascii=False))
    btype = block.get("type", "unknown")
    bs = BlockSummary(
        index=idx,
        type=btype,
        size=size,
        preview=_block_preview(block),
    )
    if btype == "tool_use":
        bs.tool_name = block.get("name")
        bs.tool_use_id = block.get("id")
    elif btype == "tool_result":
        bs.tool_result_for = block.get("tool_use_id")
        content = block.get("content", "")
        if isinstance(content, str) and content.startswith("[trimmed"):
            bs.is_trimmed = True
    return bs


def _record_preview(record: dict[str, Any], blocks: list[BlockSummary]) -> str:
    """Short preview for a record, derived from its content blocks or type."""
    rtype = record.get("type")

    # For user/assistant: use the first non-trivial block's preview
    if rtype in ("user", "assistant") and blocks:
        # Prefer text or tool_result content over tool_use/thinking
        for priority in ("text", "tool_result", "tool_use", "thinking"):
            for b in blocks:
                if b.type == priority and b.preview:
                    return b.preview
        return blocks[0].preview

    # For attachment: describe based on subtype
    if rtype == "attachment":
        att = record.get("attachment", {})
        sub = att.get("type", "?") if isinstance(att, dict) else "?"
        if sub == "file":
            filename = att.get("filename", "?")
            return f"file: {filename}"
        if sub == "skill_listing":
            return "<skill listing>"
        if sub == "invoked_skills":
            skills = att.get("skills", [])
            return f"invoked skills: {len(skills)}"
        if sub == "task_reminder":
            return "<task reminder>"
        return f"<attachment: {sub}>"

    # For system records: include subtype + content if short
    if rtype == "system":
        sub = record.get("subtype", "?")
        if sub == "compact_boundary":
            meta = record.get("compactMetadata", {}) or {}
            pre = meta.get("preTokens")
            post = meta.get("postTokens")
            trigger = meta.get("trigger", "?")
            dur_ms = meta.get("durationMs", 0)
            dur_s = (dur_ms / 1000) if dur_ms else 0
            pre_s = f"{pre:,}" if isinstance(pre, int) else "?"
            post_s = f"{post:,}" if isinstance(post, int) else "?"
            return f"compaction ({trigger}): {pre_s} → {post_s} tokens, {dur_s:.1f}s"
        content = record.get("content", "")
        if isinstance(content, str) and content:
            return f"[{sub}] {_truncate(content, 150)}"
        return f"[{sub}]"

    # Local metadata records: brief identifier
    if rtype == "file-history-snapshot":
        snap = record.get("snapshot", {})
        if isinstance(snap, dict):
            files = snap.get("trackedFileBackups", {})
            return f"<snapshot: {len(files)} files tracked>"
        return "<file-history-snapshot>"
    if rtype == "custom-title":
        return record.get("customTitle", "")[:PREVIEW_CHARS]
    if rtype == "last-prompt":
        return _truncate(record.get("lastPrompt", ""))
    if rtype == "agent-name":
        return f"agent: {record.get('agentName', '?')}"
    if rtype == "permission-mode":
        return f"mode: {record.get('permissionMode', '?')}"
    if rtype == "queue-operation":
        op = record.get("operation", "?")
        content = record.get("content", "")
        return f"{op}: {_truncate(content, 120)}"

    return f"<{rtype or 'unknown'}>"


def _extract_subtype(record: dict[str, Any]) -> str | None:
    """Return a subtype string for records that have one.

    For system records: the explicit `subtype` field.
    For attachments: the inner attachment's type.
    For user/assistant: a synthesized summary of block types present in
    message.content — single block returns its type ("text", "tool_use",
    etc.), multiple blocks return them deduplicated in priority order
    (joined by "+"), so the records list can show e.g. "text+tool_use"
    or "thinking+text" without having to expand the card.
    """
    if "subtype" in record:
        return record["subtype"]
    att = record.get("attachment")
    if isinstance(att, dict) and "type" in att:
        return att["type"]
    if record.get("type") in ("user", "assistant"):
        msg = record.get("message")
        if not isinstance(msg, dict):
            return None
        content = msg.get("content")
        if isinstance(content, str):
            return "text" if content else None
        if not isinstance(content, list):
            return None
        # Ordered priority so the output reads consistently
        priority = ("text", "thinking", "tool_use", "tool_result", "image")
        seen = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t and t not in seen:
                seen.append(t)
        if not seen:
            return None
        ordered = [t for t in priority if t in seen] + [t for t in seen if t not in priority]
        return "+".join(ordered)
    return None


PLUCK_FIELD = "_inspector_pluck"


class SessionLiveError(RuntimeError):
    """Raised when a save is attempted against a session that appears to be
    open in Claude Code. Carries the check_live() detail dict so the API can
    show the user exactly which signal fired."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}

MUTATIONS_FIELD = "_inspector_mutations"

# Placeholder used to replace trimmed string values in tool_use inputs and
# tool_result content. Chosen to be unambiguously meta-syntactic: the double
# brackets + namespace prefix make this unlikely to pattern-match against
# valid string argument values when the model sees it in subsequent turns.
# Earlier surgeries used the bare token "[trimmed]" which the model could
# confabulate into new tool calls — the namespaced form avoids that risk.
TRIM_PLACEHOLDER = "[[jsonl-inspect::trimmed]]"


def _now_iso() -> str:
    """ISO-format timestamp for mutation metadata."""
    return datetime.now().isoformat()


def _get_field(record: dict[str, Any], field_path: str) -> Any:
    """Walk a dotted path through a nested dict to fetch a value.

    Raises KeyError if any segment is missing or not a dict.
    """
    parts = field_path.split(".")
    current: Any = record
    for p in parts:
        if not isinstance(current, dict):
            raise KeyError(f"field_path '{field_path}': '{p}' is not under a dict")
        if p not in current:
            raise KeyError(f"field_path '{field_path}': key '{p}' missing")
        current = current[p]
    return current


def _set_field(record: dict[str, Any], field_path: str, value: Any) -> None:
    """Set a value at a dotted path. Intermediate dicts must already exist
    on the record — we don't auto-create them, since that would silently
    accept paths that don't make sense for the record's shape.
    """
    parts = field_path.split(".")
    current: Any = record
    for p in parts[:-1]:
        if not isinstance(current, dict) or p not in current:
            raise KeyError(f"field_path '{field_path}': intermediate '{p}' missing")
        current = current[p]
    if not isinstance(current, dict):
        raise KeyError(f"field_path '{field_path}': terminal parent not a dict")
    current[parts[-1]] = value


def _trim_tool_use_input(value: Any, threshold: int) -> tuple[Any, int]:
    """Recursively walk a tool_use.input value, replacing any string value
    longer than `threshold` bytes with TRIM_PLACEHOLDER. Returns
    `(new_value, bytes_freed)` where bytes_freed is the cumulative byte
    delta (positive when trimming happened, 0 otherwise).

    Used for trimming bulky tool_use inputs (e.g., Write.content, Edit's
    old_string/new_string) while preserving smaller fields like file_path
    naturally — they fall under the threshold so they're left alone.
    """
    if isinstance(value, str):
        if len(value) > threshold:
            return TRIM_PLACEHOLDER, len(value) - len(TRIM_PLACEHOLDER)
        return value, 0
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        freed = 0
        for k, v in value.items():
            new_v, d = _trim_tool_use_input(v, threshold)
            out[k] = new_v
            freed += d
        return out, freed
    if isinstance(value, list):
        out_list: list[Any] = []
        freed = 0
        for item in value:
            new_item, d = _trim_tool_use_input(item, threshold)
            out_list.append(new_item)
            freed += d
        return out_list, freed
    return value, 0


_TRIM_IMAGE_PLACEHOLDER_BLOCK = {
    "type": "text",
    "text": "[[jsonl-inspect::trimmed image]]",
}


def _trim_tool_result_content(
    content: Any,
    threshold: int,
    trim_images: bool = False,
) -> tuple[Any, int]:
    """Trim a tool_result.content value.

    Tool results can be either a plain string OR a list of content blocks
    (text, image, etc.). For the list form:
      - Text sub-blocks with text > threshold get replaced with the trim
        placeholder.
      - Image sub-blocks are by default PRESERVED (default `trim_images=False`),
        because the model may refer back to them and they're relatively
        cheap on tokens despite their byte size. When `trim_images=True`,
        image sub-blocks get replaced with a placeholder text block of
        type `{type: "text", text: "[[jsonl-inspect::trimmed image]]"}`.
        Block-type change is acceptable per Anthropic's tool_result schema
        — mixed types allowed in the content array.
    """
    if isinstance(content, str):
        if len(content) > threshold:
            return TRIM_PLACEHOLDER, len(content) - len(TRIM_PLACEHOLDER)
        return content, 0
    if isinstance(content, list):
        out: list[Any] = []
        freed = 0
        for block in content:
            if not isinstance(block, dict):
                out.append(block)
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and len(text) > threshold:
                    out.append({**block, "text": TRIM_PLACEHOLDER})
                    freed += len(text) - len(TRIM_PLACEHOLDER)
                else:
                    out.append(block)
            elif btype == "image" and trim_images:
                before_size = len(json.dumps(block, ensure_ascii=False))
                after_size = len(json.dumps(_TRIM_IMAGE_PLACEHOLDER_BLOCK, ensure_ascii=False))
                out.append(dict(_TRIM_IMAGE_PLACEHOLDER_BLOCK))
                freed += before_size - after_size
            else:
                out.append(block)
        return out, freed
    return content, 0


def _analyze_record(idx: int, record: dict[str, Any]) -> RecordSummary:
    rtype = record.get("type", "unknown")
    size = len(json.dumps(record, ensure_ascii=False))

    blocks: list[BlockSummary] = []
    if rtype in ("user", "assistant"):
        message = record.get("message", {})
        if isinstance(message, dict):
            content = message.get("content", [])
            if isinstance(content, list):
                for bi, block in enumerate(content):
                    if isinstance(block, dict):
                        blocks.append(_analyze_block(bi, block))
            elif isinstance(content, str) and content:
                # Anthropic API shorthand: content can be a plain string,
                # equivalent to [{"type": "text", "text": content}].
                blocks.append(BlockSummary(
                    index=0,
                    type="text",
                    size=len(json.dumps(content, ensure_ascii=False)),
                    preview=_truncate(content),
                ))

    mutations = record.get(MUTATIONS_FIELD)
    if isinstance(mutations, dict) and mutations:
        is_mutated = True
        mutated_fields = sorted(mutations.keys())
    else:
        is_mutated = False
        mutated_fields = []

    return RecordSummary(
        index=idx,
        uuid=record.get("uuid"),
        parent_uuid=record.get("parentUuid"),
        type=rtype,
        subtype=_extract_subtype(record),
        timestamp=record.get("timestamp"),
        size=size,
        is_api_relevant=(rtype in API_RELEVANT_TYPES),
        preview=_record_preview(record, blocks),
        blocks=blocks,
        is_plucked=bool(record.get(PLUCK_FIELD)),
        is_mutated=is_mutated,
        mutated_fields=mutated_fields,
    )


# ---------------------------------------------------------------------------
# Whole-session stats


def _compute_chain_reachable(records: list[dict[str, Any]]) -> set[int]:
    """Return the set of record indices reachable from the active tip via
    parentUuid walk backward.

    The active tip is the last record in file order that has a uuid (Claude
    Code's JSONL is append-only, so the tip is at the tail). Walking backward
    from the tip via parentUuid gives the records that Claude Code would
    actually send to the API on resume.

    Records not visited by this walk are out-of-chain — either soft-plucked,
    pre-compaction (parentUuid: null at the compact_boundary severs backward
    walk), or local-metadata records with no uuid.

    Earlier versions walked from "every leaf" (every record nobody points to)
    but that conflated active leaves with orphan-leaves created by plucking:
    a plucked record's parentUuid still points to its old ancestor, making it
    a "leaf" that erroneously chains backward through itself and its parents.
    """
    uuid_to_idx: dict[str, int] = {}
    for i, r in enumerate(records):
        u = r.get("uuid")
        if u:
            uuid_to_idx[u] = i

    # Walk from the tail backward, looking for the last record with a uuid.
    # That's the active conversation tip.
    tip_idx: int | None = None
    for i in range(len(records) - 1, -1, -1):
        if records[i].get("uuid"):
            tip_idx = i
            break

    if tip_idx is None:
        return set()

    reachable: set[int] = set()
    cur_idx: int | None = tip_idx
    while cur_idx is not None and cur_idx not in reachable:
        reachable.add(cur_idx)
        pu = records[cur_idx].get("parentUuid")
        cur_idx = uuid_to_idx.get(pu) if pu else None

    return reachable


def _compute_wire_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Simulate Claude Code's the wire-assembly step pipeline to identify which
    records contribute content to the wire and compute total wire bytes.

    The pipeline (observed behaviour; see docs/DESIGN-NOTES.md):
      1. chain walk: include records reachable from the active tip via
         parentUuid.
      2. message.id sibling merge: include off-chain assistant records
         that share a message.id with any in-chain assistant.
      3. tool_result parent-reattach: include off-chain user records that have
         tool_result content AND whose parentUuid points to any in-chain uuid.

    For each included user/assistant record, the wire byte cost is the JSON
    length of just `{role, content}` (mutations metadata, parentUuid,
    isSidechain, uuid, etc. are all dropped by Claude Code's local-field stripper
    before serialization to the API).

    Approximation caveats:
    - Doesn't simulate the cleanup passes (dropOrphanedThinkingOnly,
      trimTrailingThinking, the empty-content backfill pass). These make small
      adjustments to the final shape but don't materially change byte counts.
    - Doesn't account for the message.id merge step concatenating sibling content
      into one wire message (no byte difference — content blocks are the
      same, just grouped differently).
    - Doesn't include system prompt + tool definitions (~70KB fixed overhead
      per request, not derivable from the JSONL).

    Returns dict with:
    - `included_indices`: set of record indices contributing to the wire
    - `wire_bytes`: total bytes (post-mutation) of {role, content} for each
    - `inclusion_reason`: dict mapping idx → "chain" | "sibling_merge" | "parent_reattach"
    - `sibling_groups`: dict mapping message.id → list of record indices
      sharing that id (only includes groups with >1 member)
    """
    reachable = _compute_chain_reachable(records)
    in_chain_uuids = {records[i].get("uuid") for i in reachable if records[i].get("uuid")}

    # Build a message.id → indices lookup over ALL assistant records (not just
    # in-chain), so we can find off-chain siblings of in-chain assistants.
    msgid_to_indices: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        if r.get("type") != "assistant":
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        msg_id = msg.get("id")
        if msg_id:
            msgid_to_indices.setdefault(msg_id, []).append(i)

    included = set(reachable)
    inclusion_reason: dict[int, str] = {i: "chain" for i in reachable}

    # message.id sibling merge: for each in-chain assistant, pull in off-chain siblings by message.id
    for i in list(reachable):
        r = records[i]
        if r.get("type") != "assistant":
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        msg_id = msg.get("id")
        if msg_id:
            for sib_idx in msgid_to_indices.get(msg_id, []):
                if sib_idx not in included:
                    included.add(sib_idx)
                    inclusion_reason[sib_idx] = "sibling_merge"

    # tool_result parent-reattach: for each off-chain user record with tool_result content whose
    # parentUuid matches an in-chain uuid, pull it in
    for i, r in enumerate(records):
        if i in included:
            continue
        if r.get("type") != "user":
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if not any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            continue
        if r.get("parentUuid") in in_chain_uuids:
            included.add(i)
            inclusion_reason[i] = "parent_reattach"

    # Sibling groups: message.ids shared by 2+ records (regardless of chain
    # status). Useful for the inspector's #32 classification — "this record
    # has N siblings" is the signal that msg.id-mutation would matter.
    sibling_groups: dict[str, list[int]] = {
        mid: indices for mid, indices in msgid_to_indices.items()
        if len(indices) > 1
    }

    # Compute wire bytes for each included user/assistant record.
    wire_bytes = 0
    for i in included:
        r = records[i]
        if r.get("type") not in ("user", "assistant"):
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        wire_msg = {"role": msg.get("role"), "content": msg.get("content")}
        wire_bytes += len(json.dumps(wire_msg, ensure_ascii=False))

    return {
        "included_indices": included,
        "wire_bytes": wire_bytes,
        "inclusion_reason": inclusion_reason,
        "sibling_groups": sibling_groups,
    }


def _compute_stats(records: list[dict[str, Any]], summaries: list[RecordSummary]) -> SessionStats:
    # First: compute chain reachability and stamp it onto every summary so
    # the frontend can mark orphaned records.
    chain_reachable = _compute_chain_reachable(records)
    # Find the latest *active* compact_boundary — one that hasn't been plucked.
    # Plucking a compact_boundary is treated as "erasing the compaction": the
    # boundary's severing effect goes away and records before it return to
    # active-conversation status (no longer flagged pre_compaction).
    max_active_compact_idx = -1
    for s in summaries:
        if s.type == "system" and s.subtype == "compact_boundary" and not s.is_plucked:
            if s.index > max_active_compact_idx:
                max_active_compact_idx = s.index

    for summary in summaries:
        summary.is_chain_reachable = summary.index in chain_reachable
        summary.is_pre_compaction = summary.index < max_active_compact_idx
        # is_plucked was already set during _analyze_record from the
        # _inspector_pluck marker field on the record itself

    # Simulate Claude Code's wire-build pipeline to compute the actual
    # number/bytes that go on the API messages array — replaces the old
    # chain_reachable_bytes proxy (which counted disk bytes of chain-reachable
    # records and was misled by, e.g., mutations metadata adding disk bytes
    # while wire bytes shrunk).
    wire = _compute_wire_payload(records)
    wire_included = wire["included_indices"]
    wire_messages_bytes = wire["wire_bytes"]
    wire_messages_count = sum(
        1 for i in wire_included
        if records[i].get("type") in ("user", "assistant")
    )

    # Stamp wire_inclusion_reason and sibling_count onto each summary so the
    # frontend can render the reattach-mechanism badges (#32) and decide
    # which surgical mutation primitive applies (#30/#31).
    inclusion_reason = wire.get("inclusion_reason", {})
    sibling_groups = wire.get("sibling_groups", {})
    sibling_count_by_idx: dict[int, int] = {}
    for indices in sibling_groups.values():
        n = len(indices)
        for i in indices:
            sibling_count_by_idx[i] = n - 1  # others, not counting self
    for summary in summaries:
        summary.wire_inclusion_reason = inclusion_reason.get(summary.index)
        summary.sibling_count = sibling_count_by_idx.get(summary.index, 0)

    type_counts: Counter[str] = Counter()
    block_type_counts: Counter[str] = Counter()
    bytes_by_type: Counter[str] = Counter()
    bytes_by_block_type: Counter[str] = Counter()
    total_bytes = 0
    api_bytes = 0
    orphan_record_indices: list[int] = []
    record_sizes: list[tuple[int, int]] = []
    block_sizes: list[tuple[int, int, str, int]] = []

    tool_use_ids: dict[str, int] = {}      # tool_use_id -> record_idx
    tool_result_refs: dict[str, int] = {}  # tool_use_id -> record_idx referencing it

    for summary in summaries:
        type_counts[summary.type] += 1
        bytes_by_type[summary.type] += summary.size
        total_bytes += summary.size
        if summary.is_api_relevant:
            api_bytes += summary.size
            # "Orphan" = API-relevant record that doesn't contribute to the wire
            # (i.e., not chain-reachable AND not pulled in by sibling-merge or
            # tool_result reattach). This is stricter than the old "not chain-
            # reachable" definition, since reattached off-chain records DO go
            # on the wire.
            if summary.index not in wire_included:
                orphan_record_indices.append(summary.index)
        record_sizes.append((summary.index, summary.size))
        for block in summary.blocks:
            block_type_counts[block.type] += 1
            bytes_by_block_type[block.type] += block.size
            block_sizes.append((summary.index, block.index, block.type, block.size))
            if block.type == "tool_use" and block.tool_use_id:
                tool_use_ids[block.tool_use_id] = summary.index
            elif block.type == "tool_result" and block.tool_result_for:
                tool_result_refs[block.tool_result_for] = summary.index

    record_sizes.sort(key=lambda t: -t[1])
    block_sizes.sort(key=lambda t: -t[3])

    # Parent-chain validation
    uuid_set = {s.uuid for s in summaries if s.uuid}
    orphans: list[tuple[int, str]] = []
    for s in summaries:
        if s.parent_uuid and s.parent_uuid not in uuid_set:
            # Forked-from sessions are a legitimate exception
            rec = records[s.index]
            forked = rec.get("forkedFrom", {})
            if isinstance(forked, dict) and forked.get("messageUuid") == s.parent_uuid:
                continue
            orphans.append((s.index, s.parent_uuid))

    # Tool pairing validation
    tool_use_set = set(tool_use_ids)
    tool_result_set = set(tool_result_refs)
    use_without_result = sorted(tool_use_set - tool_result_set)
    result_without_use = sorted(tool_result_set - tool_use_set)

    # Compaction events
    compaction_events: list[dict[str, Any]] = []
    for s in summaries:
        if s.type == "system" and s.subtype == "compact_boundary":
            rec = records[s.index]
            meta = rec.get("compactMetadata", {}) or {}
            compaction_events.append({
                "index": s.index,
                "uuid": s.uuid,
                "timestamp": s.timestamp,
                "trigger": meta.get("trigger"),
                "pre_tokens": meta.get("preTokens"),
                "post_tokens": meta.get("postTokens"),
                "duration_ms": meta.get("durationMs"),
            })

    # Latest in-chain assistant's usage — the preflight cache signal.
    latest_input_tokens = None
    latest_input_tokens_idx = None
    latest_cache_creation_tokens = None
    latest_cache_read_tokens = None
    for i in sorted(chain_reachable, reverse=True):
        if records[i].get("type") != "assistant":
            continue
        msg = records[i].get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        latest_input_tokens = usage.get("input_tokens")
        latest_input_tokens_idx = i
        latest_cache_creation_tokens = usage.get("cache_creation_input_tokens")
        latest_cache_read_tokens = usage.get("cache_read_input_tokens")
        break

    return SessionStats(
        total_records=len(records),
        total_bytes=total_bytes,
        api_bytes=api_bytes,
        wire_messages_count=wire_messages_count,
        wire_messages_bytes=wire_messages_bytes,
        orphan_record_indices=orphan_record_indices,
        type_counts=dict(type_counts),
        block_type_counts=dict(block_type_counts),
        bytes_by_type=dict(bytes_by_type),
        bytes_by_block_type=dict(bytes_by_block_type),
        top_records_by_size=record_sizes[:50],
        top_blocks_by_size=block_sizes[:50],
        orphan_parent_uuids=orphans,
        tool_use_without_result=use_without_result,
        tool_result_without_use=result_without_use,
        compaction_events=compaction_events,
        latest_input_tokens=latest_input_tokens,
        latest_input_tokens_idx=latest_input_tokens_idx,
        latest_cache_creation_tokens=latest_cache_creation_tokens,
        latest_cache_read_tokens=latest_cache_read_tokens,
    )


# ---------------------------------------------------------------------------
# Public session object


class Session:
    """In-memory representation of a Claude Code JSONL session file.

    Holds both parsed records (for analysis) and the original raw lines
    (for byte-faithful writeback of untouched records). Claude Code mixes
    default and compact JSON formatting across record types, so naively
    re-serializing every record on save would grow the file unnecessarily.
    """

    def __init__(
        self,
        file_path: Path,
        records: list[dict[str, Any]],
        summaries: list[RecordSummary],
        stats: SessionStats,
        raw_lines: list[str],
    ) -> None:
        self.file_path = file_path
        self.records = records
        self.summaries = summaries
        self.stats = stats
        self._raw_lines = raw_lines
        # Snapshot of the raw lines as they exist on disk (post last save).
        # `_raw_lines` is the *working* state; mutations modify it without
        # touching `_saved_raw_lines`. discard_all() copies saved back.
        self._saved_raw_lines: list[str] = list(raw_lines)
        # Wire size as of the last saved state. Pending edits stage across
        # many requests, so save() must report against THIS, not against
        # whatever the size happened to be when save was called.
        self._saved_wire_bytes: int = stats.wire_messages_bytes
        # Operations staged since last save, in apply order. Each entry has
        # enough info to be inverted for undo and reported for display.
        # See _record_op for the schema per op type.
        self._pending_ops: list[dict[str, Any]] = []
        self._modified: set[int] = set()

    @classmethod
    def load(cls, path: str | Path) -> "Session":
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"JSONL file not found: {file_path}")

        records: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        with file_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON on line {line_no}: {e}") from e
                raw_lines.append(line)

        summaries = [_analyze_record(i, r) for i, r in enumerate(records)]
        stats = _compute_stats(records, summaries)
        session = cls(file_path, records, summaries, stats, raw_lines)
        # Baseline for live-session detection: if the file changes underneath
        # us, Claude Code (or something else) is still appending to it.
        session._load_mtime = file_path.stat().st_mtime
        session._load_size = file_path.stat().st_size
        return session

    def get_record(self, idx: int) -> dict[str, Any]:
        if not 0 <= idx < len(self.records):
            raise IndexError(f"Record index out of range: {idx}")
        return self.records[idx]

    # ---- Backup browsing / restore ------------------------------------

    def list_backups(self) -> list[dict[str, Any]]:
        """List timestamped backups alongside the canonical file.

        Backup naming: <stem>.backup-YYYYMMDD-HHMMSS[-mmm]<suffix>.
        Milliseconds are optional (legacy backups predate the ms suffix).
        Returns sorted most-recent-first by mtime.
        """
        stem = self.file_path.stem
        suffix = self.file_path.suffix
        pattern = re.compile(
            rf"^{re.escape(stem)}\.backup-(\d{{8}})-(\d{{6}})(?:-(\d{{3}}))?{re.escape(suffix)}$"
        )
        candidates: list[dict[str, Any]] = []
        for p in self.file_path.parent.iterdir():
            if not p.is_file():
                continue
            m = pattern.match(p.name)
            if not m:
                continue
            try:
                base_ts = datetime.strptime(m.group(1) + "-" + m.group(2), "%Y%m%d-%H%M%S")
                if m.group(3):
                    base_ts = base_ts.replace(microsecond=int(m.group(3)) * 1000)
            except ValueError:
                continue
            stat = p.stat()
            candidates.append({
                "path": str(p),
                "filename": p.name,
                "timestamp": base_ts.isoformat(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
        # Sort by the filename-derived timestamp, not by mtime. shutil.copy2
        # preserves source mtime, so a backup's mtime reflects when the
        # canonical state was last saved, not when the backup was created.
        # After a restore, the canonical's mtime jumps backward and any
        # subsequent backup inherits that older mtime — out-of-order in a
        # by-mtime sort. The filename timestamp is the source of truth.
        candidates.sort(key=lambda x: x["timestamp"], reverse=True)
        return candidates

    def restore_backup(self, backup_path: str) -> dict[str, Any]:
        """Restore the canonical file from a named backup. Creates a fresh
        backup of the current canonical state first (so the restore itself
        is reversible). Then re-loads in-memory state from disk.

        Validates that the backup is in the same directory and matches the
        expected backup-naming pattern for this session — prevents arbitrary
        file copies.
        """
        bp = Path(backup_path).expanduser().resolve()
        if bp.parent != self.file_path.parent:
            raise ValueError("backup must be in the same directory as the canonical file")
        stem = self.file_path.stem
        suffix = self.file_path.suffix
        pattern = re.compile(
            rf"^{re.escape(stem)}\.backup-\d{{8}}-\d{{6}}(?:-\d{{3}})?{re.escape(suffix)}$"
        )
        if not pattern.match(bp.name):
            raise ValueError("not a recognized backup file for this session")
        if not bp.is_file():
            raise FileNotFoundError(f"backup not found: {bp}")

        # 1) Snapshot current canonical so the restore is itself reversible
        pre_restore_backup = _backup_file(self.file_path)
        # 2) Copy backup over canonical
        shutil.copy2(bp, self.file_path)
        # 3) Re-load all in-memory state from disk
        self._reload_from_disk()
        return {"pre_restore_backup": str(pre_restore_backup)}

    def _reload_from_disk(self) -> None:
        """Re-read the canonical file and reset all in-memory state. Used
        after restoring from a backup. Discards any pending changes."""
        records: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        with self.file_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON on line {line_no}: {e}") from e
                raw_lines.append(line)
        summaries = [_analyze_record(i, r) for i, r in enumerate(records)]
        self.records = records
        self._raw_lines = raw_lines
        self._saved_raw_lines = list(raw_lines)
        self._pending_ops = []
        self._modified.clear()
        self.summaries = summaries
        self.stats = _compute_stats(self.records, self.summaries)
        self._saved_wire_bytes = self.stats.wire_messages_bytes

    def find_pluck_set(self, seed: int | list[int]) -> list[int]:
        """Return the set of indices that must be plucked together with the
        seed indices to avoid orphaning any tool_use ↔ tool_result references.

        Accepts either a single index or a list (for bulk plucks). BFS over the
        pair graph from all seeds and returns the union. Cascades through
        assistant turns that contain multiple tool_uses whose results live in
        different user records.
        """
        if isinstance(seed, int):
            seeds = [seed]
        else:
            seeds = list(seed)
        if not seeds:
            return []
        for i in seeds:
            if not 0 <= i < len(self.records):
                raise IndexError(f"Record index out of range: {i}")

        # tool_use_id -> idx of the record containing that tool_use block
        tool_use_to_idx: dict[str, int] = {}
        for i, summary in enumerate(self.summaries):
            for block in summary.blocks:
                if block.type == "tool_use" and block.tool_use_id:
                    tool_use_to_idx[block.tool_use_id] = i

        # idx -> set of paired idx (undirected: tool_use<->tool_result)
        pair_links: dict[int, set[int]] = {}
        for i, summary in enumerate(self.summaries):
            for block in summary.blocks:
                if block.type == "tool_result" and block.tool_result_for:
                    other = tool_use_to_idx.get(block.tool_result_for)
                    if other is not None and other != i:
                        pair_links.setdefault(i, set()).add(other)
                        pair_links.setdefault(other, set()).add(i)

        pluck_set: set[int] = set(seeds)
        stack: list[int] = list(seeds)
        while stack:
            cur = stack.pop()
            for neighbor in pair_links.get(cur, ()):
                if neighbor not in pluck_set:
                    pluck_set.add(neighbor)
                    stack.append(neighbor)

        return sorted(pluck_set)

    def pluck_records(self, indices: list[int]) -> dict[str, Any]:
        """Stage a holistic unlink: remove records from ALL three wire-inclusion
        mechanisms simultaneously.

        For each non-already-plucked record in `indices`:
          1. Mutate `message.id` to a fresh UUID (breaks the message.id sibling-merge —
             other records sharing the original id no longer pull this one
             in via toolResultsByParent lookups for its uuid as parent).
          2. Mutate `parentUuid` to a fresh non-resolvable UUID (breaks the tool_result parent-reattach
             parent-reattach AND removes this record from chain walks rooted
             at any record that was its old ancestor).
          3. Apply `_do_pluck`: add the _inspector_pluck marker and re-thread
             children whose parentUuid pointed at this record (the existing
             chain-walk handling).

        compact_boundary records skip steps (1) and (2): they have no msg.id,
        their parentUuid is intentionally null, and their `_do_pluck` carries
        special chain-bridging logic. Records already carrying the
        _inspector_pluck marker are skipped to avoid clobbering prior metadata.

        Single staged op in the pending log; one undo reverses everything.
        """
        if not indices:
            raise ValueError("pluck_records called with empty indices")
        for i in indices:
            if not 0 <= i < len(self.records):
                raise IndexError(f"Record index out of range: {i}")

        # Filter out indices already plucked; tolerant — they're already off-wire.
        indices_to_apply = [
            i for i in indices
            if not isinstance(self.records[i].get(PLUCK_FIELD), dict)
        ]
        if not indices_to_apply:
            return {
                "plucked_indices": [],
                "children_updated": [],
                "field_mutations": [],
                "skipped_already_plucked": sorted(set(indices)),
            }

        # Step 1: chain rethread first. resolve_new_parent in _do_pluck reads
        # records' CURRENT parentUuids when walking through plucked ancestors,
        # so we must do this BEFORE mutating any parentUuid below. Otherwise
        # children would land on the fresh-unlinked sentinel instead of the
        # plucked record's original parent.
        pluck_result = self._do_pluck(indices_to_apply)

        # Step 2: apply field mutations (msg.id + parentUuid) to each plucked
        # record. Skip compact_boundary records (they have no msg.id and a
        # deliberately-null parentUuid).
        import uuid as _uuid
        field_mutations: list[dict[str, Any]] = []
        for idx in indices_to_apply:
            rec = self.records[idx]
            is_compact_boundary = (
                rec.get("type") == "system"
                and rec.get("subtype") == "compact_boundary"
            )
            if is_compact_boundary:
                # Per the load-time prune at compact boundaries (see
                # 05-compaction-loadtime-prune.js): on session load, Claude
                # Code wipes the transcript Map of everything before each
                # compact_boundary entry without preservation metadata. The
                # chain walker then never sees the pre-compact tail, no
                # matter how we re-thread parentUuids. The kX(b) marker
                # check is `subtype === "compact_boundary"` — so changing
                # the subtype defuses the prune entirely. The bridge-chain
                # logic in _do_pluck still re-threads children appropriately.
                had_prior = (
                    isinstance(rec.get(MUTATIONS_FIELD), dict)
                    and "subtype" in rec[MUTATIONS_FIELD]
                )
                self._do_mutate(idx, "subtype", "compact_boundary_unlinked", "unlink_pluck")
                field_mutations.append({
                    "idx": idx,
                    "field_path": "subtype",
                    "before_value": "compact_boundary",
                    "had_prior_mutation": had_prior,
                    "uuid": rec.get("uuid"),
                })
                continue

            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("id"):
                before_id = msg["id"]
                new_id = f"msg_inspector_unlinked_{_uuid.uuid4().hex[:24]}"
                had_prior = (
                    isinstance(rec.get(MUTATIONS_FIELD), dict)
                    and "message.id" in rec[MUTATIONS_FIELD]
                )
                self._do_mutate(idx, "message.id", new_id, "unlink_pluck")
                field_mutations.append({
                    "idx": idx,
                    "field_path": "message.id",
                    "before_value": before_id,
                    "had_prior_mutation": had_prior,
                    "uuid": rec.get("uuid"),
                })

            if rec.get("parentUuid") is not None:
                before_parent = rec["parentUuid"]
                new_parent = f"00000000-inspector-unlinked-{_uuid.uuid4().hex[:12]}"
                had_prior = (
                    isinstance(rec.get(MUTATIONS_FIELD), dict)
                    and "parentUuid" in rec[MUTATIONS_FIELD]
                )
                self._do_mutate(idx, "parentUuid", new_parent, "unlink_pluck")
                field_mutations.append({
                    "idx": idx,
                    "field_path": "parentUuid",
                    "before_value": before_parent,
                    "had_prior_mutation": had_prior,
                    "uuid": rec.get("uuid"),
                })

        # Step 3: embed field_mutations in each affected record's pluck marker
        # so unpluck (a separate code path from undo_last) can reverse them.
        if field_mutations:
            muts_by_uuid: dict[str, list[dict[str, Any]]] = {}
            for entry in field_mutations:
                muts_by_uuid.setdefault(entry["uuid"], []).append(entry)
            for idx in pluck_result["plucked_indices"]:
                rec_uuid = self.records[idx].get("uuid")
                if rec_uuid not in muts_by_uuid:
                    continue
                marker = self.records[idx].get(PLUCK_FIELD)
                if not isinstance(marker, dict):
                    continue
                marker["field_mutations"] = muts_by_uuid[rec_uuid]
                self._raw_lines[idx] = json.dumps(self.records[idx], ensure_ascii=False)
                self._modified.add(idx)

        self._pending_ops.append({
            "type": "pluck",
            "plucked_indices": pluck_result["plucked_indices"],
            "children_updated": pluck_result["children_updated"],
            "field_mutations": field_mutations,
        })
        self._refresh_stats()
        pluck_result["field_mutations"] = field_mutations
        return pluck_result

    def _do_pluck(self, indices: list[int]) -> dict[str, Any]:
        """Low-level mutator for pluck. Modifies records/raw_lines/summaries
        but does not log an op or write to disk.

        Only handles the chain-walk concern: rethreads children's
        parentUuid to skip plucked records, adds the _inspector_pluck marker.
        The public `pluck_records` does additional msg.id/parentUuid mutations
        (for the sibling-merge and parent-reattach mechanisms) and embeds them into the
        marker post-hoc, so all the unpluck-time recovery info is colocated."""
        plucked_set = set(indices)
        uuid_to_idx: dict[str, int] = {
            r["uuid"]: i for i, r in enumerate(self.records) if r.get("uuid")
        }
        plucked_uuids = {
            self.records[i].get("uuid")
            for i in indices
            if self.records[i].get("uuid")
        }

        # Special case: plucking a compact_boundary is treated as "un-sever the
        # chain across this boundary," not "leave the chain truncated." The
        # boundary's null parentUuid was the artificial break; the user's
        # intent in plucking it is to restore the pre-compaction records to
        # chain. For each plucked compact_boundary, find the pre-compaction
        # tip (latest user/assistant/attachment record before the boundary
        # with a uuid) and override the rethread target so that any child
        # pointing into the boundary lands on that tip instead of walking to
        # null.
        compact_overrides: dict[str, str | None] = {}
        for i in plucked_set:
            rec = self.records[i]
            if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                boundary_uuid = rec.get("uuid")
                if not boundary_uuid:
                    continue
                tip_uuid: str | None = None
                for j in range(i - 1, -1, -1):
                    r = self.records[j]
                    if r.get("uuid") and r.get("type") in ("user", "assistant", "attachment"):
                        tip_uuid = r["uuid"]
                        break
                if tip_uuid is not None:
                    compact_overrides[boundary_uuid] = tip_uuid

        def resolve_new_parent(pu: str | None) -> str | None:
            while pu is not None and pu in plucked_uuids:
                if pu in compact_overrides:
                    return compact_overrides[pu]
                ancestor_idx = uuid_to_idx.get(pu)
                if ancestor_idx is None:
                    return None
                pu = self.records[ancestor_idx].get("parentUuid")
            return pu

        rethreads_log: list[dict[str, str]] = []
        children_updated: list[int] = []
        for i, rec in enumerate(self.records):
            if i in plucked_set:
                continue
            pu = rec.get("parentUuid")
            if pu and pu in plucked_uuids:
                new_pu = resolve_new_parent(pu)
                rec["parentUuid"] = new_pu
                self._raw_lines[i] = json.dumps(rec, ensure_ascii=False)
                self._modified.add(i)
                self.summaries[i].parent_uuid = new_pu
                children_updated.append(i)
                child_uuid = rec.get("uuid")
                if child_uuid:
                    rethreads_log.append({
                        "child_uuid": child_uuid,
                        "old_parent_uuid": pu,
                    })

        # Per-record markers, grouped by a shared batch_id. Each plucked
        # record stores ONLY the rethreads attributable to it — i.e. children
        # whose original direct parent (old_parent_uuid, always a plucked uuid)
        # was THIS record. This keeps each marker O(1) in size as the batch
        # grows, so a bulk pluck of N records costs O(N) on disk, not O(N^2).
        #
        # The prior form wrote the whole batch's plucked_uuids + rethreads list
        # into every record's marker; a real 1824-record bulk pluck ballooned
        # the session file to 330MB (1824 x ~178KB). unpluck now reconstructs
        # the full group by scanning for records sharing the batch_id.
        batch_id = uuid.uuid4().hex
        rethreads_by_owner: dict[str, list[dict[str, str]]] = {}
        for entry in rethreads_log:
            rethreads_by_owner.setdefault(entry["old_parent_uuid"], []).append(entry)
        for i in plucked_set:
            rec = self.records[i]
            rec_uuid = rec.get("uuid")
            rec[PLUCK_FIELD] = {
                "batch_id": batch_id,
                "rethreads": rethreads_by_owner.get(rec_uuid, []),
            }
            self._raw_lines[i] = json.dumps(rec, ensure_ascii=False)
            self._modified.add(i)
            self.summaries[i].is_plucked = True

        return {
            "plucked_indices": sorted(plucked_set),
            "children_updated": sorted(children_updated),
        }

    def unpluck_record(self, idx: int) -> dict[str, Any]:
        """Stage an un-pluck (reverse a pluck operation). Mutates in-memory
        state only; call save() to flush."""
        if not 0 <= idx < len(self.records):
            raise IndexError(f"Record index out of range: {idx}")
        meta = self.records[idx].get(PLUCK_FIELD)
        if not meta:
            raise ValueError("Record is not plucked")
        # Save the pluck metadata before _do_unpluck strips it, so undo can
        # re-apply it.
        result = self._do_unpluck(idx)
        self._pending_ops.append({
            "type": "unpluck",
            "from_idx": idx,
            "unplucked_indices": result["unplucked_indices"],
            "children_restored": result["children_restored"],
            "saved_metadata": meta,
        })
        self._refresh_stats()
        return result

    def _do_unpluck(self, idx: int) -> dict[str, Any]:
        """Low-level mutator for unpluck. Reads the `_inspector_pluck` metadata
        from the clicked record, restores re-threaded children's parentUuids,
        reverses any embedded field_mutations (msg.id, parentUuid) applied as
        part of the holistic unlink, and removes the pluck marker from every
        record in the pluck group.

        Does not log an op or write to disk."""
        rec = self.records[idx]
        meta = rec.get(PLUCK_FIELD)
        if not meta:
            raise ValueError("Record is not plucked")

        # Reconstruct the full pluck group. Two marker formats supported:
        #  - current (batch_id): each record's marker holds only its own
        #    rethreads + field_mutations; the group is every record sharing
        #    this batch_id. Union their pieces to reverse the whole batch.
        #  - legacy (plucked_uuids): the whole batch's plucked_uuids + rethreads
        #    were duplicated onto every record (the O(N^2) form that ballooned
        #    files). The clicked record's marker already holds everything.
        batch_id = meta.get("batch_id")
        if batch_id is not None:
            plucked_uuids = set()
            rethreads = []
            field_mutations = []
            for r in self.records:
                m = r.get(PLUCK_FIELD)
                if isinstance(m, dict) and m.get("batch_id") == batch_id:
                    ru = r.get("uuid")
                    if ru:
                        plucked_uuids.add(ru)
                    rethreads.extend(m.get("rethreads", []))
                    field_mutations.extend(m.get("field_mutations", []))
        else:
            plucked_uuids = set(meta.get("plucked_uuids", []))
            rethreads = meta.get("rethreads", [])
            field_mutations = meta.get("field_mutations", [])

        uuid_to_idx: dict[str, int] = {
            r["uuid"]: i for i, r in enumerate(self.records) if r.get("uuid")
        }

        # Reverse field mutations FIRST so the records are back to their
        # original msg.id / parentUuid before the chain rethread restore.
        # Use the embedded uuid to find the record (idx may have shifted).
        for entry in reversed(field_mutations):
            uuid_lookup = entry.get("uuid")
            target_idx = uuid_to_idx.get(uuid_lookup) if uuid_lookup else entry.get("idx")
            if target_idx is None or not (0 <= target_idx < len(self.records)):
                continue
            self._reverse_mutate_entry(
                target_idx,
                entry["field_path"],
                entry["before_value"],
                entry.get("had_prior_mutation", False),
            )
            # uuid_to_idx may have changed if we restored a uuid above; rebuild
            # only if the field we restored was an identifying one.
            if entry["field_path"] in ("uuid", "message.id"):
                uuid_to_idx = {
                    r["uuid"]: i for i, r in enumerate(self.records) if r.get("uuid")
                }

        children_restored: list[int] = []
        for entry in rethreads:
            child_uuid = entry.get("child_uuid")
            old_parent_uuid = entry.get("old_parent_uuid")
            ci = uuid_to_idx.get(child_uuid)
            if ci is None:
                continue
            self.records[ci]["parentUuid"] = old_parent_uuid
            self._raw_lines[ci] = json.dumps(self.records[ci], ensure_ascii=False)
            self._modified.add(ci)
            self.summaries[ci].parent_uuid = old_parent_uuid
            children_restored.append(ci)

        unplucked_indices: list[int] = []
        for u in plucked_uuids:
            pi = uuid_to_idx.get(u)
            if pi is None:
                continue
            pr = self.records[pi]
            pr.pop(PLUCK_FIELD, None)
            self._raw_lines[pi] = json.dumps(pr, ensure_ascii=False)
            self._modified.add(pi)
            self.summaries[pi].is_plucked = False
            unplucked_indices.append(pi)

        return {
            "unplucked_indices": sorted(unplucked_indices),
            "children_restored": sorted(children_restored),
        }

    def update_record(
        self,
        idx: int,
        new_record: dict[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> RecordSummary:
        """Stage an edit. Mutates in-memory state only; call save() to flush.
        Lenient validation: only ensures new_record is a dict and JSON-encodable.

        Optional `warnings` is the client-side validation list (each entry
        `{"severity": "warn"|"info", "text": "..."}`) — stored with the op
        so the pending-changes modal can surface them later.
        """
        if not 0 <= idx < len(self.records):
            raise IndexError(f"Record index out of range: {idx}")
        if not isinstance(new_record, dict):
            raise ValueError("new_record must be a JSON object (dict)")
        json.dumps(new_record, ensure_ascii=False)

        before = json.loads(json.dumps(self.records[idx]))
        new_summary = self._do_edit(idx, new_record)
        op_entry: dict[str, Any] = {
            "type": "edit",
            "idx": idx,
            "before": before,
        }
        if warnings:
            op_entry["warnings"] = warnings
        self._pending_ops.append(op_entry)
        self._refresh_stats()
        return new_summary

    def _do_edit(self, idx: int, new_record: dict[str, Any]) -> RecordSummary:
        """Low-level mutator for edit. Replaces record at idx with new_record,
        re-serializes, re-analyzes. Does not log an op or write to disk."""
        self.records[idx] = new_record
        self._raw_lines[idx] = json.dumps(new_record, ensure_ascii=False)
        self._modified.add(idx)
        new_summary = _analyze_record(idx, new_record)
        self.summaries[idx] = new_summary
        return new_summary

    # ---- Field-level mutations (reversible) -----------------------------

    def mutate_field(
        self,
        idx: int,
        field_path: str,
        new_value: Any,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Stage a reversible field-level mutation.

        Records the field's current value as the baseline in
        `_inspector_mutations[field_path]` (only on the first mutation of a
        field; re-mutations preserve the true baseline), then sets the field
        to `new_value`. Mutates in-memory state only; call save() to flush.
        """
        if not 0 <= idx < len(self.records):
            raise IndexError(f"Record index out of range: {idx}")
        # Validate the new value is JSON-encodable up front
        json.dumps(new_value, ensure_ascii=False)
        rec = self.records[idx]
        try:
            before_value = _get_field(rec, field_path)
        except KeyError as e:
            raise ValueError(str(e)) from e
        had_prior_mutation = (
            isinstance(rec.get(MUTATIONS_FIELD), dict)
            and field_path in rec[MUTATIONS_FIELD]
        )
        result = self._do_mutate(idx, field_path, new_value, reason)
        self._pending_ops.append({
            "type": "mutate",
            "idx": idx,
            "field_path": field_path,
            "before_value": before_value,
            "had_prior_mutation": had_prior_mutation,
            "reason": reason,
        })
        self._refresh_stats()
        return result

    def _do_mutate(
        self,
        idx: int,
        field_path: str,
        new_value: Any,
        reason: str | None,
    ) -> dict[str, Any]:
        """Low-level mutator. Writes the mutations metadata entry (preserving
        true baseline on re-mutate), sets the field, regenerates the raw line,
        and re-analyzes the record. Does not log an op or write to disk."""
        rec = self.records[idx]
        mutations = rec.setdefault(MUTATIONS_FIELD, {})
        if field_path not in mutations:
            mutations[field_path] = {
                "original": _get_field(rec, field_path),
                "mutated_at": _now_iso(),
                "reason": reason or "manual",
            }
        else:
            mutations[field_path]["mutated_at"] = _now_iso()
            if reason:
                mutations[field_path]["reason"] = reason
        _set_field(rec, field_path, new_value)
        self._raw_lines[idx] = json.dumps(rec, ensure_ascii=False)
        self._modified.add(idx)
        self.summaries[idx] = _analyze_record(idx, rec)
        return {"idx": idx, "field_path": field_path}

    def unmutate_field(self, idx: int, field_path: str) -> dict[str, Any]:
        """Stage restoration of a single field to its baseline.

        Reads the baseline from `_inspector_mutations[field_path].original`,
        applies it, and removes the mutation entry (collapsing the
        `_inspector_mutations` dict if it becomes empty).
        """
        if not 0 <= idx < len(self.records):
            raise IndexError(f"Record index out of range: {idx}")
        rec = self.records[idx]
        mutations = rec.get(MUTATIONS_FIELD) or {}
        if field_path not in mutations:
            raise ValueError(f"field '{field_path}' has no mutation to undo")
        before_value = _get_field(rec, field_path)
        saved_mutation = json.loads(json.dumps(mutations[field_path]))
        result = self._do_unmutate(idx, field_path)
        self._pending_ops.append({
            "type": "unmutate",
            "idx": idx,
            "field_path": field_path,
            "before_value": before_value,
            "saved_mutation": saved_mutation,
        })
        self._refresh_stats()
        return result

    def _reverse_mutate_entry(
        self,
        idx: int,
        field_path: str,
        before_value: Any,
        had_prior_mutation: bool,
    ) -> None:
        """Undo helper: restore a field to before_value, and remove the
        mutations entry iff it didn't exist before this mutation.
        Used by undo_last for both 'mutate' and 'bulk_mutate' op types."""
        rec = self.records[idx]
        _set_field(rec, field_path, before_value)
        if not had_prior_mutation:
            mutations = rec.get(MUTATIONS_FIELD) or {}
            mutations.pop(field_path, None)
            if not mutations:
                rec.pop(MUTATIONS_FIELD, None)
        self._raw_lines[idx] = json.dumps(rec, ensure_ascii=False)
        self._modified.add(idx)
        self.summaries[idx] = _analyze_record(idx, rec)

    def _do_unmutate(self, idx: int, field_path: str) -> dict[str, Any]:
        rec = self.records[idx]
        mutations = rec.get(MUTATIONS_FIELD) or {}
        if field_path not in mutations:
            raise ValueError(f"field '{field_path}' has no mutation to undo")
        baseline = mutations[field_path]["original"]
        _set_field(rec, field_path, baseline)
        del mutations[field_path]
        if not mutations:
            del rec[MUTATIONS_FIELD]
        self._raw_lines[idx] = json.dumps(rec, ensure_ascii=False)
        self._modified.add(idx)
        self.summaries[idx] = _analyze_record(idx, rec)
        return {"idx": idx, "field_path": field_path}

    def _bulk_mutate(
        self,
        plan: list[tuple[int, str, Any]],
        reason: str,
        extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply many mutations as a single staged op.

        Each (idx, field_path, new_value) entry in `plan` goes through
        `_do_mutate` so the per-record metadata is consistent, but the
        operations accumulate into one pending_ops entry of type
        'bulk_mutate'. A single undo reverses the whole batch.
        """
        applied: list[dict[str, Any]] = []
        for idx, field_path, new_value in plan:
            if not 0 <= idx < len(self.records):
                continue
            rec = self.records[idx]
            try:
                before_value = _get_field(rec, field_path)
            except KeyError:
                continue  # tolerant: skip records that lack the field
            had_prior_mutation = (
                isinstance(rec.get(MUTATIONS_FIELD), dict)
                and field_path in rec[MUTATIONS_FIELD]
            )
            self._do_mutate(idx, field_path, new_value, reason)
            applied.append({
                "idx": idx,
                "field_path": field_path,
                "before_value": before_value,
                "had_prior_mutation": had_prior_mutation,
            })
        op: dict[str, Any] = {
            "type": "bulk_mutate",
            "reason": reason,
            "mutations": applied,
        }
        if extras:
            op.update(extras)
        self._pending_ops.append(op)
        self._refresh_stats()
        return {"n_mutated": len(applied), "reason": reason}

    # ---- Quick actions (high-level, built on _bulk_mutate) --------------

    def strip_thinking_blocks(self, keep_last_n: int = 1) -> dict[str, Any]:
        """Strip thinking blocks from chain-reachable assistant records.

        Preserves the N most-recent reachable assistants intact (defensive:
        the API may rely on the most-recent thinking for continuity).
        Idempotent: records whose `message.content` is already mutated are
        skipped, so re-runs are no-ops.
        """
        if keep_last_n < 0:
            raise ValueError("keep_last_n must be >= 0")

        reachable = _compute_chain_reachable(self.records)
        assistant_indices = sorted(
            i for i in reachable
            if self.records[i].get("type") == "assistant"
        )
        preserved = set(assistant_indices[-keep_last_n:]) if keep_last_n > 0 else set()

        plan: list[tuple[int, str, Any]] = []
        bytes_freed_est = 0
        for i in assistant_indices:
            if i in preserved:
                continue
            rec = self.records[i]
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            mutations = rec.get(MUTATIONS_FIELD) or {}
            if "message.content" in mutations:
                continue  # idempotent
            if not any(
                isinstance(b, dict)
                and b.get("type") in ("thinking", "redacted_thinking")
                for b in content
            ):
                continue
            new_content = [
                b for b in content
                if not (
                    isinstance(b, dict)
                    and b.get("type") in ("thinking", "redacted_thinking")
                )
            ]
            before_bytes = len(json.dumps(content, ensure_ascii=False))
            after_bytes = len(json.dumps(new_content, ensure_ascii=False))
            bytes_freed_est += before_bytes - after_bytes
            plan.append((i, "message.content", new_content))

        if not plan:
            return {
                "n_mutated": 0,
                "preserved_indices": sorted(preserved),
                "bytes_freed_est": 0,
                "reason": "strip_thinking_quick_action",
                "keep_last_n": keep_last_n,
            }

        result = self._bulk_mutate(
            plan,
            reason="strip_thinking_quick_action",
            extras={
                "keep_last_n": keep_last_n,
                "preserved_indices": sorted(preserved),
                "bytes_freed_est": bytes_freed_est,
            },
        )
        result["preserved_indices"] = sorted(preserved)
        result["bytes_freed_est"] = bytes_freed_est
        result["keep_last_n"] = keep_last_n
        return result

    def strip_images(self, keep_last_n: int = 2) -> dict[str, Any]:
        """Replace pasted image blocks in chain-reachable records with a
        short placeholder, leaving any accompanying text untouched.

        This targets images sitting directly in a message's content array —
        screenshots pasted into a turn — which nothing else reached:
        `trim_tool_calls` only looks inside tool_use/tool_result blocks, and
        plucking the record would take the user's text with it.

        Images are byte-dense but token-cheap (~194 bytes/token vs ~4 for
        text), so this reclaims far more file size than context. It's still
        worth having: on a real session, 51 pasted images were 87% of the
        wire bytes and ~71k tokens, all of it live on the chain.

        A placeholder is left in place of each image rather than dropping the
        block, so the turn still shows that something was attached — same
        reasoning as trimming tool calls instead of removing them.

        `keep_last_n` preserves the N most-recent *image-bearing* records
        (the screenshots most likely still under discussion) rather than the
        N most-recent records overall, which often carry no image at all.

        Idempotent: records whose `message.content` is already mutated are
        skipped, so re-runs are no-ops.
        """
        if keep_last_n < 0:
            raise ValueError("keep_last_n must be >= 0")

        def _has_image(content: Any) -> bool:
            return isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "image" for b in content
            )

        reachable = _compute_chain_reachable(self.records)
        image_indices = sorted(
            i for i in reachable
            if isinstance(self.records[i].get("message"), dict)
            and _has_image(self.records[i]["message"].get("content"))
        )
        preserved = set(image_indices[-keep_last_n:]) if keep_last_n > 0 else set()

        plan: list[tuple[int, str, Any]] = []
        bytes_freed_est = 0
        n_images = 0
        for i in image_indices:
            if i in preserved:
                continue
            rec = self.records[i]
            mutations = rec.get(MUTATIONS_FIELD) or {}
            if "message.content" in mutations:
                continue  # idempotent
            content = rec["message"]["content"]
            new_content = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image":
                    new_content.append(dict(_TRIM_IMAGE_PLACEHOLDER_BLOCK))
                    n_images += 1
                else:
                    new_content.append(b)
            before_bytes = len(json.dumps(content, ensure_ascii=False))
            after_bytes = len(json.dumps(new_content, ensure_ascii=False))
            bytes_freed_est += before_bytes - after_bytes
            plan.append((i, "message.content", new_content))

        if not plan:
            return {
                "n_mutated": 0,
                "n_images": 0,
                "preserved_indices": sorted(preserved),
                "bytes_freed_est": 0,
                "reason": "strip_images_quick_action",
                "keep_last_n": keep_last_n,
            }

        result = self._bulk_mutate(
            plan,
            reason="strip_images_quick_action",
            extras={
                "keep_last_n": keep_last_n,
                "preserved_indices": sorted(preserved),
                "bytes_freed_est": bytes_freed_est,
                "n_images": n_images,
            },
        )
        result["preserved_indices"] = sorted(preserved)
        result["bytes_freed_est"] = bytes_freed_est
        result["keep_last_n"] = keep_last_n
        result["n_images"] = n_images
        return result

    def trim_tool_calls(
        self,
        keep_last_n: int = 3,
        threshold_bytes: int = 500,
        trim_images: bool = False,
    ) -> dict[str, Any]:
        """Trim bulky strings in tool_use inputs and tool_result content
        across chain-reachable user/assistant records.

        Preserves the last N records intact (default 3 — keeps recent tool
        call context useful for the model). Strings longer than
        `threshold_bytes` get replaced with TRIM_PLACEHOLDER; smaller fields
        like file paths and short command strings pass through untouched.

        If `trim_images=True`, image sub-blocks inside tool_result content
        are also replaced with a placeholder text block. By default images
        are preserved (they're cheap on tokens despite their byte size,
        and the model may refer back to them). Opt in when image-bearing
        tool_results dominate the byte budget (often the case in vision /
        camera-injection sessions).

        Idempotent: records whose `message.content` is already mutated are
        skipped, so re-runs are no-ops.
        """
        if keep_last_n < 0:
            raise ValueError("keep_last_n must be >= 0")
        if threshold_bytes < 0:
            raise ValueError("threshold_bytes must be >= 0")

        reachable = _compute_chain_reachable(self.records)
        user_asst_indices = sorted(
            i for i in reachable
            if self.records[i].get("type") in ("user", "assistant")
        )
        preserved = (
            set(user_asst_indices[-keep_last_n:]) if keep_last_n > 0 else set()
        )

        plan: list[tuple[int, str, Any]] = []
        bytes_freed_est = 0

        for i in user_asst_indices:
            if i in preserved:
                continue
            rec = self.records[i]
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            mutations = rec.get(MUTATIONS_FIELD) or {}
            if "message.content" in mutations:
                continue  # idempotent

            new_content: list[Any] = []
            record_freed = 0
            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    trimmed_input, freed = _trim_tool_use_input(
                        block.get("input"), threshold_bytes
                    )
                    if freed > 0:
                        new_content.append({**block, "input": trimmed_input})
                        record_freed += freed
                    else:
                        new_content.append(block)
                elif btype == "tool_result":
                    trimmed_content, freed = _trim_tool_result_content(
                        block.get("content"), threshold_bytes, trim_images=trim_images
                    )
                    if freed > 0:
                        new_content.append({**block, "content": trimmed_content})
                        record_freed += freed
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)

            if record_freed > 0:
                plan.append((i, "message.content", new_content))
                bytes_freed_est += record_freed

        if not plan:
            return {
                "n_mutated": 0,
                "preserved_indices": sorted(preserved),
                "bytes_freed_est": 0,
                "reason": "trim_tool_calls_quick_action",
                "threshold_bytes": threshold_bytes,
                "keep_last_n": keep_last_n,
                "trim_images": trim_images,
            }

        result = self._bulk_mutate(
            plan,
            reason="trim_tool_calls_quick_action",
            extras={
                "keep_last_n": keep_last_n,
                "threshold_bytes": threshold_bytes,
                "trim_images": trim_images,
                "preserved_indices": sorted(preserved),
                "bytes_freed_est": bytes_freed_est,
            },
        )
        result["preserved_indices"] = sorted(preserved)
        result["bytes_freed_est"] = bytes_freed_est
        result["keep_last_n"] = keep_last_n
        result["threshold_bytes"] = threshold_bytes
        result["trim_images"] = trim_images
        return result

    def refresh_preflight_usage(
        self,
        new_input_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Mutate `message.usage` on the latest in-chain assistant record to
        bypass Claude Code's preflight context-window check.

        The preflight reads `usage.input_tokens` (and possibly the related
        cache_* counts) from the most recent assistant response to estimate
        context usage BEFORE firing a new API request. If you've recently
        plucked/trimmed records to shrink the wire payload, the cached
        usage values are stale (still reflecting the pre-trim payload),
        which can block new requests even though the actual API call would
        be smaller.

        This op resets:
          - usage.input_tokens → `new_input_tokens` (default: estimated
            wire tokens, computed as wire_messages_bytes / 4)
          - usage.cache_creation_input_tokens → 0
          - usage.cache_read_input_tokens → 0

        After the next real API call, Anthropic's response will populate
        fresh usage values that overwrite the tampered ones (Claude Code
        writes each new assistant turn with its own usage). So this is
        effectively a one-shot bypass.

        Reversible: stored via _inspector_mutations so undo restores the
        original values.
        """
        reachable = _compute_chain_reachable(self.records)
        target_idx: int | None = None
        for i in sorted(reachable, reverse=True):
            if self.records[i].get("type") == "assistant":
                target_idx = i
                break
        if target_idx is None:
            raise ValueError("no in-chain assistant record found")

        rec = self.records[target_idx]
        msg = rec.get("message")
        if not isinstance(msg, dict):
            raise ValueError("latest in-chain assistant has no message dict")
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("latest in-chain assistant has no usage dict")

        # Auto-default for the new input_tokens value: roughly the estimated
        # wire tokens (wire_messages_bytes / 4). At least 1 to guarantee a
        # believable non-zero value.
        if new_input_tokens is None:
            new_input_tokens = max(1, self.stats.wire_messages_bytes // 4)
        if not isinstance(new_input_tokens, int) or new_input_tokens < 0:
            raise ValueError("new_input_tokens must be a non-negative int")

        before = {
            "input_tokens": usage.get("input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        }

        # Build new usage dict — keep any unrelated fields (output_tokens,
        # cache_creation breakdown, service_tier, iterations, etc.) intact,
        # only override the three preflight-relevant counts. Deep-copy via
        # json roundtrip so the mutation's stored "original" is independent
        # of our in-place modifications.
        new_usage = json.loads(json.dumps(usage))
        new_usage["input_tokens"] = new_input_tokens
        new_usage["cache_creation_input_tokens"] = 0
        new_usage["cache_read_input_tokens"] = 0
        # If there's a cache_creation breakdown dict, zero it too.
        if isinstance(new_usage.get("cache_creation"), dict):
            new_usage["cache_creation"] = {
                k: 0 for k in new_usage["cache_creation"]
            }

        self.mutate_field(
            target_idx,
            "message.usage",
            new_usage,
            reason="refresh_preflight_usage_quick_action",
        )

        return {
            "n_mutated": 1,
            "target_idx": target_idx,
            "before_usage": before,
            "after_input_tokens": new_input_tokens,
            "reason": "refresh_preflight_usage_quick_action",
        }

    def break_sibling_merge(self, idx: int) -> dict[str, Any]:
        """Holistic unlink via the sibling-merge entry point: delegates to
        `pluck_records([idx])`. This applies the full unlink (chain rethread
        + msg.id mutation + parentUuid mutation), not just msg.id alone.

        Rationale (#35): pluck and the two break-* operations are all
        user-intent-equivalent — "remove this record from the wire". Each
        previously addressed only one of the three reattach mechanisms,
        leaving the record reachable via the others. The unified operation
        breaks all three at once.

        The badge UI still uses this entry point to surface why a record
        is on the wire (sibling-merge classification), but the action is
        the same as a pluck."""
        if not 0 <= idx < len(self.records):
            raise IndexError(f"Record index out of range: {idx}")
        return self.pluck_records([idx])

    def break_parent_reattach(self, idx: int) -> dict[str, Any]:
        """Holistic unlink via the parent-reattach entry point: delegates to
        `pluck_records([idx])`. See break_sibling_merge for rationale (#35)."""
        if not 0 <= idx < len(self.records):
            raise IndexError(f"Record index out of range: {idx}")
        return self.pluck_records([idx])

    # ---- Save / Discard / Undo / Pending --------------------------------

    def _refresh_stats(self) -> None:
        """Recompute stats from current working state and reflect pending count."""
        self.stats = _compute_stats(self.records, self.summaries)
        self.stats.pending_ops_count = len(self._pending_ops)

    def has_pending_changes(self) -> bool:
        return bool(self._pending_ops)

    def get_pending_ops(self) -> list[dict[str, Any]]:
        """Return a frontend-friendly view of pending ops (without bulky
        undo metadata)."""
        out: list[dict[str, Any]] = []
        for op in self._pending_ops:
            t = op["type"]
            if t == "pluck":
                out.append({
                    "type": "pluck",
                    "plucked_indices": op["plucked_indices"],
                    "children_updated": op["children_updated"],
                })
            elif t == "unpluck":
                out.append({
                    "type": "unpluck",
                    "unplucked_indices": op["unplucked_indices"],
                    "children_restored": op["children_restored"],
                })
            elif t == "edit":
                entry: dict[str, Any] = {"type": "edit", "idx": op["idx"]}
                if op.get("warnings"):
                    entry["warnings"] = op["warnings"]
                out.append(entry)
            elif t == "mutate":
                out.append({
                    "type": "mutate",
                    "idx": op["idx"],
                    "field_path": op["field_path"],
                    "reason": op.get("reason"),
                })
            elif t == "unmutate":
                out.append({
                    "type": "unmutate",
                    "idx": op["idx"],
                    "field_path": op["field_path"],
                })
            elif t == "bulk_mutate":
                out.append({
                    "type": "bulk_mutate",
                    "reason": op["reason"],
                    "n_mutations": len(op["mutations"]),
                    "indices": sorted({e["idx"] for e in op["mutations"]}),
                    "bytes_freed_est": op.get("bytes_freed_est"),
                    "preserved_indices": op.get("preserved_indices"),
                    "keep_last_n": op.get("keep_last_n"),
                })
        return out

    def check_live(self) -> dict[str, Any]:
        """Detect whether this session file is currently open in Claude Code.

        Editing a live session is the one genuinely destructive mistake this
        tool allows: Claude Code holds the transcript in memory and appends to
        it, so our whole-file write either gets clobbered by its next append or
        corrupts the file outright. Worse, it *looks* like the edit silently
        failed, which sends people hunting for a bug that isn't there.

        Two independent signals, either of which is enough to warn:

        - **File changed since load** — mtime or size advanced while we've been
          holding it. Something else is writing. Cheap and portable.
        - **A process has the file open** — `lsof` on the path. Stronger and
          more immediate (catches a live session that just hasn't written yet),
          but platform-dependent, so failures here are non-fatal.

        Returns a dict the API/UI can render directly. `is_live` is the
        conservative OR of both signals.
        """
        result: dict[str, Any] = {
            "is_live": False,
            "file_changed": False,
            "process_holding": False,
            "detail": "",
            "holders": [],
        }
        try:
            st = self.file_path.stat()
        except OSError:
            result["detail"] = "file is no longer readable"
            result["is_live"] = True
            return result

        baseline_mtime = getattr(self, "_load_mtime", None)
        baseline_size = getattr(self, "_load_size", None)
        if baseline_mtime is not None and (
            st.st_mtime > baseline_mtime or st.st_size != baseline_size
        ):
            result["file_changed"] = True

        try:
            proc = subprocess.run(
                ["lsof", "-t", "--", str(self.file_path)],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p for p in proc.stdout.split() if p.strip()]
            if pids:
                names = []
                for pid in pids[:8]:
                    try:
                        nm = subprocess.run(
                            ["ps", "-p", pid, "-o", "comm="],
                            capture_output=True, text=True, timeout=2,
                        ).stdout.strip()
                    except (OSError, subprocess.SubprocessError):
                        nm = ""
                    names.append(f"{nm or '?'} (pid {pid})")
                result["process_holding"] = True
                result["holders"] = names
        except (OSError, subprocess.SubprocessError):
            # lsof missing or blocked — fall back to the mtime signal alone.
            pass

        result["is_live"] = result["file_changed"] or result["process_holding"]
        if result["is_live"]:
            bits = []
            if result["process_holding"]:
                bits.append("a process currently has this file open ("
                            + ", ".join(result["holders"]) + ")")
            if result["file_changed"]:
                bits.append("the file changed on disk since it was loaded here")
            result["detail"] = "; ".join(bits)
        return result

    def refresh_live_baseline(self) -> None:
        """Re-baseline the live-detection markers to the file's current state.

        Called after our own save, so our write doesn't look like someone
        else's append on the next check.
        """
        try:
            st = self.file_path.stat()
            self._load_mtime = st.st_mtime
            self._load_size = st.st_size
        except OSError:
            pass

    def save(self, force: bool = False) -> dict[str, Any]:
        """Flush staged changes to disk atomically with a single backup of
        the pre-save state. Returns the count of ops saved + backup path.

        Refuses to write if the session looks live (open in Claude Code),
        since that risks clobbering or corrupting the transcript. Pass
        `force=True` to override deliberately.
        """
        if not self._pending_ops:
            return {"ops_saved": 0, "backup_path": None}
        if not force:
            live = self.check_live()
            if live["is_live"]:
                raise SessionLiveError(
                    "This session appears to be open in Claude Code — "
                    f"{live['detail']}. Saving now would be overwritten by the "
                    "running session (or corrupt the file). Close the session "
                    "in Claude Code, then save again.",
                    live,
                )
        backup_path = _backup_file(self.file_path)
        wire_before = getattr(self, "_saved_wire_bytes", self.stats.wire_messages_bytes)
        _atomic_write_jsonl(self.file_path, self._raw_lines)
        self.refresh_live_baseline()
        self._saved_raw_lines = list(self._raw_lines)
        ops_saved = len(self._pending_ops)
        self._pending_ops = []
        self._refresh_stats()
        self._saved_wire_bytes = self.stats.wire_messages_bytes
        return {
            "ops_saved": ops_saved,
            "backup_path": str(backup_path),
            "wire_bytes_before": wire_before,
            "wire_bytes_after": self.stats.wire_messages_bytes,
        }

    def discard_all(self) -> dict[str, Any]:
        """Revert all pending changes by restoring records/raw_lines/summaries
        from the saved snapshot. No disk write."""
        if not self._pending_ops:
            return {"ops_discarded": 0}
        ops_discarded = len(self._pending_ops)
        self._raw_lines = list(self._saved_raw_lines)
        self.records = [json.loads(line) for line in self._raw_lines]
        self.summaries = [_analyze_record(i, r) for i, r in enumerate(self.records)]
        self._pending_ops = []
        self._modified.clear()
        self._refresh_stats()
        return {"ops_discarded": ops_discarded}

    def undo_last(self) -> dict[str, Any]:
        """Pop and reverse the last staged operation."""
        if not self._pending_ops:
            raise ValueError("nothing to undo")
        op = self._pending_ops.pop()
        t = op["type"]
        affected_indices: list[int] = []
        if t == "edit":
            self._do_edit(op["idx"], op["before"])
            affected_indices = [op["idx"]]
        elif t == "pluck":
            # Reverse: undo in inverse order — unpluck first (restores chain
            # rethread + clears markers), then reverse each field mutation
            # (msg.id, parentUuid) using its captured before_value. The
            # field_mutations list is the new bookkeeping from holistic pluck;
            # pre-unification pluck ops won't have it, so default to [].
            target_idx = op["plucked_indices"][0] if op["plucked_indices"] else None
            if target_idx is not None:
                result = self._do_unpluck(target_idx)
                affected_indices = result["unplucked_indices"] + result["children_restored"]
            for entry in reversed(op.get("field_mutations", [])):
                self._reverse_mutate_entry(
                    entry["idx"], entry["field_path"],
                    entry["before_value"], entry["had_prior_mutation"],
                )
                affected_indices.append(entry["idx"])
        elif t == "unpluck":
            # Reverse: re-pluck using the saved metadata. We do _do_pluck on
            # the original indices, which recomputes the same chain re-thread.
            uuid_to_idx: dict[str, int] = {
                r["uuid"]: i for i, r in enumerate(self.records) if r.get("uuid")
            }
            meta = op.get("saved_metadata") or {}
            plucked_uuids = meta.get("plucked_uuids", [])
            indices = sorted(
                uuid_to_idx[u] for u in plucked_uuids if u in uuid_to_idx
            )
            if indices:
                result = self._do_pluck(indices)
                affected_indices = result["plucked_indices"] + result["children_updated"]
        elif t == "mutate":
            # Reverse: restore field to before_value. If this mutation created
            # the _inspector_mutations entry (had_prior_mutation=False),
            # remove the entry too — the field is back to its true baseline.
            self._reverse_mutate_entry(
                op["idx"], op["field_path"],
                op["before_value"], op["had_prior_mutation"],
            )
            affected_indices = [op["idx"]]
        elif t == "unmutate":
            # Reverse: restore field to before_value AND re-create the
            # mutation entry from saved_mutation.
            rec = self.records[op["idx"]]
            _set_field(rec, op["field_path"], op["before_value"])
            mutations = rec.setdefault(MUTATIONS_FIELD, {})
            mutations[op["field_path"]] = op["saved_mutation"]
            self._raw_lines[op["idx"]] = json.dumps(rec, ensure_ascii=False)
            self._modified.add(op["idx"])
            self.summaries[op["idx"]] = _analyze_record(op["idx"], rec)
            affected_indices = [op["idx"]]
        elif t == "bulk_mutate":
            # Reverse: walk mutations list in reverse, undo each.
            for entry in reversed(op["mutations"]):
                self._reverse_mutate_entry(
                    entry["idx"], entry["field_path"],
                    entry["before_value"], entry["had_prior_mutation"],
                )
                affected_indices.append(entry["idx"])
        else:
            # Shouldn't happen but keep a sentinel
            raise ValueError(f"unknown pending op type: {t}")
        self._refresh_stats()
        return {
            "undone_op_type": t,
            "affected_indices": sorted(set(affected_indices)),
        }


# ---------------------------------------------------------------------------
# File I/O helpers (atomic write + backup)


def _backup_file(path: Path) -> Path:
    """Copy <path> to <path>.backup-YYYYMMDD-HHMMSS-mmm.<ext> alongside the
    original. Milliseconds included so rapid back-to-back saves don't collide
    on the same filename and overwrite each other."""
    now = datetime.now()
    ms = now.microsecond // 1000
    timestamp = now.strftime("%Y%m%d-%H%M%S") + f"-{ms:03d}"
    backup_path = path.parent / f"{path.stem}.backup-{timestamp}{path.suffix}"
    shutil.copy2(path, backup_path)
    return backup_path


def _atomic_write_jsonl(path: Path, lines: list[str]) -> None:
    """Write JSON-encoded lines to path atomically via temp-file + rename.

    Takes pre-serialized lines so callers can preserve original byte-level
    formatting for unmodified records (Claude Code mixes default and compact
    JSON formatting across record types).
    """
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        # Preserve original permissions if possible
        try:
            orig_mode = path.stat().st_mode
            os.chmod(tmp_path, orig_mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file on failure
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
