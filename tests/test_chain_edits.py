"""Smoke tests for chain-editing operations: pluck, mutate, and the
quick-action bulk operations built on top.

Each test builds a small synthetic JSONL in a tmp dir, loads it through
Session, performs operations, and verifies both in-memory state and the
on-disk file shape after save. These exercise the round-trip integrity
that almost slipped past us in phase 3."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jsonl_inspect.parser import (
    MUTATIONS_FIELD,
    PLUCK_FIELD,
    TRIM_PLACEHOLDER,
    Session,
    SessionLiveError,
)


# ---------------------------------------------------------------------------
# Fixtures: synthetic JSONL builders


def _make_record(
    uuid: str,
    rtype: str,
    parent: str | None,
    *,
    msg_id: str | None = None,
    content=None,
    extra: dict | None = None,
) -> dict:
    rec: dict = {
        "type": rtype,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": f"2026-01-01T00:00:0{uuid[-1]}Z",
        "isSidechain": False,
    }
    if rtype in ("user", "assistant"):
        msg: dict = {"role": rtype, "content": content if content is not None else []}
        if msg_id:
            msg["id"] = msg_id
        if rtype == "assistant":
            msg["usage"] = {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 1000,
            }
        rec["message"] = msg
    if extra:
        rec.update(extra)
    return rec


def write_session(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture
def simple_chain(tmp_path: Path) -> Path:
    """3-turn chain: u1 -> a1 -> u2 -> a2 -> u3 -> a3."""
    records = [
        _make_record("u1", "user", None, content=[{"type": "text", "text": "hi"}]),
        _make_record("a1", "assistant", "u1",
                     msg_id="msg_a1",
                     content=[{"type": "text", "text": "hello"}]),
        _make_record("u2", "user", "a1",
                     content=[{"type": "text", "text": "second turn"}]),
        _make_record("a2", "assistant", "u2",
                     msg_id="msg_a2",
                     content=[
                         {"type": "thinking", "thinking": "let me think", "signature": "SIG2"},
                         {"type": "text", "text": "answer 2"},
                     ]),
        _make_record("u3", "user", "a2",
                     content=[{"type": "text", "text": "third turn"}]),
        _make_record("a3", "assistant", "u3",
                     msg_id="msg_a3",
                     content=[
                         {"type": "thinking", "thinking": "more thinking", "signature": "SIG3"},
                         {"type": "text", "text": "answer 3"},
                     ]),
    ]
    p = tmp_path / "simple_chain.jsonl"
    write_session(p, records)
    return p


@pytest.fixture
def chain_with_tool_calls(tmp_path: Path) -> Path:
    """Chain where assistants make tool calls and users return tool_results."""
    big_content = "x" * 2000  # > 500B threshold
    records = [
        _make_record("u1", "user", None, content=[{"type": "text", "text": "do a thing"}]),
        _make_record("a1", "assistant", "u1",
                     msg_id="msg_a1",
                     content=[
                         {"type": "text", "text": "Doing it"},
                         {"type": "tool_use", "id": "tu1", "name": "Read",
                          "input": {"file_path": "/short/path.txt"}},
                     ]),
        _make_record("u2", "user", "a1", content=[
            {"type": "tool_result", "tool_use_id": "tu1", "content": big_content},
        ]),
        _make_record("a2", "assistant", "u2",
                     msg_id="msg_a2",
                     content=[
                         {"type": "tool_use", "id": "tu2", "name": "Write",
                          "input": {"file_path": "/p.txt", "content": big_content}},
                     ]),
        _make_record("u3", "user", "a2", content=[
            {"type": "tool_result", "tool_use_id": "tu2", "content": "ok"},
        ]),
    ]
    p = tmp_path / "with_tool_calls.jsonl"
    write_session(p, records)
    return p


# ---------------------------------------------------------------------------
# pluck round-trip


def test_pluck_then_unpluck_restores_chain(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    assert s.summaries[2].uuid == "u2"
    # Pluck u2 → re-threads u2's direct child a2 to skip u2 (a2.parent: u2 → a1)
    s.pluck_records([2])
    assert s.summaries[2].is_plucked
    a2 = s.records[3]
    assert a2["parentUuid"] == "a1", "a2.parentUuid should bypass plucked u2"
    # Save and reload — state persists
    s.save()
    s2 = Session.load(simple_chain)
    assert s2.summaries[2].is_plucked
    assert s2.records[3]["parentUuid"] == "a1"
    # Un-pluck — a2.parent restored to u2, marker cleared
    s2.unpluck_record(2)
    assert not s2.summaries[2].is_plucked
    assert s2.records[3]["parentUuid"] == "u2"
    assert PLUCK_FIELD not in s2.records[2]


def test_pluck_compact_boundary_resurrects_pre_compact(tmp_path: Path) -> None:
    records = [
        _make_record("u1", "user", None, content=[{"type": "text", "text": "old"}]),
        _make_record("a1", "assistant", "u1",
                     msg_id="msg_a1",
                     content=[{"type": "text", "text": "old reply"}]),
        # compact_boundary severs chain
        {"type": "system", "uuid": "cb1", "subtype": "compact_boundary",
         "parentUuid": None, "timestamp": "2026-01-01T00:01:00Z",
         "compactMetadata": {"trigger": "manual", "preTokens": 100, "postTokens": 10}},
        _make_record("u2", "user", "cb1", content=[{"type": "text", "text": "new"}]),
        _make_record("a2", "assistant", "u2",
                     msg_id="msg_a2",
                     content=[{"type": "text", "text": "new reply"}]),
    ]
    p = tmp_path / "with_compact.jsonl"
    write_session(p, records)
    s = Session.load(p)

    # Before pluck: chain reachable from a2 -> u2 -> cb1 (parent=None stops there).
    # u1, a1 are NOT reachable.
    reachable_before = {i for i, summ in enumerate(s.summaries) if summ.is_chain_reachable}
    assert 0 not in reachable_before  # u1
    assert 1 not in reachable_before  # a1

    # Pluck the compact_boundary: chain should re-thread u2.parent → a1's uuid
    # (pre-compaction tip), and the boundary's subtype gets mutated so Claude
    # Code's load-time prune no longer treats this entry as a prune trigger
    # (see docs/DESIGN-NOTES.md for the mechanism).
    s.pluck_records([2])
    u2 = s.records[3]
    assert u2["parentUuid"] == "a1", f"u2.parentUuid should bridge to a1, got {u2['parentUuid']}"
    # Subtype mutation defuses the load-time prune at compact boundaries
    boundary = s.records[2]
    assert boundary["subtype"] == "compact_boundary_unlinked"
    # Bridge + subtype mutation recorded in the pluck marker for un-pluck recovery
    marker = boundary[PLUCK_FIELD]
    fm = marker.get("field_mutations", [])
    assert any(e["field_path"] == "subtype" for e in fm)
    # Now u1 and a1 reachable via chain walk
    s._refresh_stats()
    assert s.summaries[0].is_chain_reachable
    assert s.summaries[1].is_chain_reachable


# ---------------------------------------------------------------------------
# mutate + unmutate round-trip


def test_mutate_field_writes_metadata_and_persists(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    s.mutate_field(3, "message.content", [], reason="strip_test")
    rec = s.records[3]
    assert rec["message"]["content"] == []
    assert MUTATIONS_FIELD in rec
    assert "message.content" in rec[MUTATIONS_FIELD]
    # Original should hold thinking + text blocks
    original = rec[MUTATIONS_FIELD]["message.content"]["original"]
    assert any(b.get("type") == "thinking" for b in original)
    assert any(b.get("type") == "text" for b in original)

    # Save and reload — metadata persists
    s.save()
    s2 = Session.load(simple_chain)
    assert s2.summaries[3].is_mutated
    assert "message.content" in s2.summaries[3].mutated_fields


def test_re_mutate_preserves_baseline(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    s.mutate_field(3, "message.content", [], reason="first")
    rec = s.records[3]
    baseline1 = rec[MUTATIONS_FIELD]["message.content"]["original"]
    s.mutate_field(3, "message.content", [{"type": "text", "text": "X"}], reason="second")
    baseline2 = rec[MUTATIONS_FIELD]["message.content"]["original"]
    assert baseline1 == baseline2, "baseline must survive across re-mutations"


def test_unmutate_restores_baseline(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    original = json.loads(json.dumps(s.records[3]["message"]["content"]))
    s.mutate_field(3, "message.content", [], reason="test")
    s.unmutate_field(3, "message.content")
    assert s.records[3]["message"]["content"] == original
    assert MUTATIONS_FIELD not in s.records[3]


# ---------------------------------------------------------------------------
# undo_last for each op type


def test_undo_last_reverses_mutate(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    original = json.loads(json.dumps(s.records[3]["message"]["content"]))
    s.mutate_field(3, "message.content", [], reason="t")
    s.undo_last()
    assert s.records[3]["message"]["content"] == original
    assert MUTATIONS_FIELD not in s.records[3]


def test_undo_last_reverses_bulk_mutate(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    pre = json.loads(json.dumps(s.records))
    s.strip_thinking_blocks(keep_last_n=0)
    s.undo_last()
    for i, (before, after) in enumerate(zip(pre, s.records)):
        assert before == after, f"record {i} not fully restored after bulk_mutate undo"


def test_undo_last_reverses_pluck(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    pre = json.loads(json.dumps(s.records))
    s.pluck_records([2])
    s.undo_last()
    for i, (before, after) in enumerate(zip(pre, s.records)):
        assert before == after, f"record {i} not restored after pluck undo"


# ---------------------------------------------------------------------------
# bulk quick actions


def test_strip_thinking_blocks_keeps_last_n(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    # Two thinking records: a2 (idx 3) and a3 (idx 5). keep_last_n=1 preserves a3.
    result = s.strip_thinking_blocks(keep_last_n=1)
    assert result["n_mutated"] == 1
    assert 5 in result["preserved_indices"]
    # a2 should have no thinking blocks anymore
    a2_blocks = s.records[3]["message"]["content"]
    assert not any(b.get("type") == "thinking" for b in a2_blocks)
    # a3 untouched
    a3_blocks = s.records[5]["message"]["content"]
    assert any(b.get("type") == "thinking" for b in a3_blocks)


def test_strip_thinking_is_idempotent(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    s.strip_thinking_blocks(keep_last_n=0)
    second = s.strip_thinking_blocks(keep_last_n=0)
    assert second["n_mutated"] == 0


def test_trim_tool_calls_preserves_paths(chain_with_tool_calls: Path) -> None:
    s = Session.load(chain_with_tool_calls)
    s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    # Find the Write tool_use; its file_path should still be the short string
    # (file_path = "/p.txt"), content field should be trimmed.
    a2 = s.records[3]["message"]["content"]
    tool_use = [b for b in a2 if b.get("type") == "tool_use"][0]
    assert tool_use["input"]["file_path"] == "/p.txt"
    assert tool_use["input"]["content"] == TRIM_PLACEHOLDER


def test_trim_tool_calls_preserves_images_by_default(tmp_path: Path) -> None:
    """tool_result image sub-blocks are preserved unless trim_images=True."""
    fake_image_data = "X" * 5000  # > threshold
    records = [
        _make_record("u1", "user", None, content=[{"type": "text", "text": "show me"}]),
        _make_record("a1", "assistant", "u1",
                     msg_id="msg_a1",
                     content=[
                         {"type": "tool_use", "id": "tu1", "name": "Read",
                          "input": {"file_path": "/x.png"}},
                     ]),
        _make_record("u2", "user", "a1", content=[
            {"type": "tool_result", "tool_use_id": "tu1", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": fake_image_data}},
            ]},
        ]),
        _make_record("a2", "assistant", "u2",
                     msg_id="msg_a2",
                     content=[{"type": "text", "text": "ok"}]),
    ]
    p = tmp_path / "with_image_tool_result.jsonl"
    write_session(p, records)

    s = Session.load(p)
    s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)  # default: trim_images=False
    u2 = s.records[2]["message"]["content"]
    tr = [b for b in u2 if b.get("type") == "tool_result"][0]
    # Image preserved
    assert tr["content"][0]["type"] == "image"
    assert tr["content"][0]["source"]["data"] == fake_image_data


def test_trim_tool_calls_trims_images_when_opted_in(tmp_path: Path) -> None:
    """With trim_images=True, image sub-blocks become placeholder text blocks."""
    fake_image_data = "X" * 5000
    records = [
        _make_record("u1", "user", None, content=[{"type": "text", "text": "show me"}]),
        _make_record("a1", "assistant", "u1",
                     msg_id="msg_a1",
                     content=[
                         {"type": "tool_use", "id": "tu1", "name": "Read",
                          "input": {"file_path": "/x.png"}},
                     ]),
        _make_record("u2", "user", "a1", content=[
            {"type": "tool_result", "tool_use_id": "tu1", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": fake_image_data}},
            ]},
        ]),
        _make_record("a2", "assistant", "u2",
                     msg_id="msg_a2",
                     content=[{"type": "text", "text": "ok"}]),
    ]
    p = tmp_path / "with_image_to_trim.jsonl"
    write_session(p, records)

    s = Session.load(p)
    result = s.trim_tool_calls(keep_last_n=0, threshold_bytes=500, trim_images=True)
    u2 = s.records[2]["message"]["content"]
    tr = [b for b in u2 if b.get("type") == "tool_result"][0]
    # Image replaced with placeholder text block
    assert tr["content"][0]["type"] == "text"
    assert tr["content"][0]["text"] == "[[jsonl-inspect::trimmed image]]"
    assert result["trim_images"] is True
    assert result["bytes_freed_est"] > 0


def test_trim_tool_calls_trims_tool_result_strings(chain_with_tool_calls: Path) -> None:
    s = Session.load(chain_with_tool_calls)
    s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    # u2's tool_result content (string form, 2000 chars) should be trimmed
    u2 = s.records[2]["message"]["content"]
    tr = [b for b in u2 if b.get("type") == "tool_result"][0]
    assert tr["content"] == TRIM_PLACEHOLDER


def test_refresh_preflight_usage_zeroes_cache(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    # Latest in-chain assistant is a3 (idx 5)
    result = s.refresh_preflight_usage(new_input_tokens=42)
    assert result["target_idx"] == 5
    usage = s.records[5]["message"]["usage"]
    assert usage["input_tokens"] == 42
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


# ---------------------------------------------------------------------------
# wire-payload computation accuracy


def test_wire_messages_bytes_excludes_local_metadata(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    # The wire bytes should equal the sum of {role, content} JSON sizes, not
    # the sum of full record sizes (which include uuid, parentUuid, etc.).
    expected = 0
    for r in s.records:
        if r.get("type") not in ("user", "assistant"):
            continue
        if not r.get("uuid") or r["uuid"] not in {"u1", "a1", "u2", "a2", "u3", "a3"}:
            continue
        msg = r["message"]
        expected += len(json.dumps({"role": msg["role"], "content": msg["content"]}, ensure_ascii=False))
    assert s.stats.wire_messages_bytes == expected


def test_wire_bytes_drops_after_strip(simple_chain: Path) -> None:
    s = Session.load(simple_chain)
    before = s.stats.wire_messages_bytes
    s.strip_thinking_blocks(keep_last_n=0)
    after = s.stats.wire_messages_bytes
    assert after < before, f"wire bytes should drop after strip-thinking: before={before}, after={after}"


# ---------------------------------------------------------------------------
# message.id sibling merge inclusion


def test_pluck_also_mutates_msg_id_and_parent_uuid(simple_chain: Path) -> None:
    """Holistic unlink (#35): plucking should additionally mutate the
    plucked record's message.id and parentUuid to break the message.id sibling-merge
    and parent-reattach respectively, alongside the chain rethread."""
    s = Session.load(simple_chain)
    a2 = s.records[3]
    original_msg_id = a2["message"]["id"]
    original_parent = a2["parentUuid"]
    assert original_msg_id == "msg_a2"
    assert original_parent == "u2"

    s.pluck_records([3])
    # message.id should be mutated to a fresh inspector-prefixed value
    new_msg_id = a2["message"]["id"]
    assert new_msg_id != original_msg_id
    assert new_msg_id.startswith("msg_inspector_unlinked_")
    # parentUuid should be mutated to a fresh sentinel
    assert a2["parentUuid"] != original_parent
    assert a2["parentUuid"].startswith("00000000-inspector-unlinked-")
    # The pluck marker should embed the field_mutations for unpluck recovery
    marker = a2[PLUCK_FIELD]
    fm = marker.get("field_mutations", [])
    paths = sorted(e["field_path"] for e in fm)
    assert paths == ["message.id", "parentUuid"]


def test_unpluck_reverses_field_mutations(simple_chain: Path) -> None:
    """When the user clicks un-pluck on a holistically-plucked record, both
    the chain rethread AND the msg.id/parentUuid mutations get reversed."""
    s = Session.load(simple_chain)
    a2 = s.records[3]
    original_msg_id = a2["message"]["id"]
    original_parent = a2["parentUuid"]

    s.pluck_records([3])
    s.save()  # persist
    s2 = Session.load(simple_chain)
    s2.unpluck_record(3)

    rec = s2.records[3]
    assert rec["message"]["id"] == original_msg_id, "msg.id should be restored"
    assert rec["parentUuid"] == original_parent, "parentUuid should be restored"
    assert PLUCK_FIELD not in rec, "pluck marker should be removed"
    # Mutations dict should also be cleaned up since baselines are restored
    assert MUTATIONS_FIELD not in rec, f"mutations dict should be empty/removed: {rec.get(MUTATIONS_FIELD)}"


def test_undo_last_reverses_holistic_pluck(simple_chain: Path) -> None:
    """undo_last on a holistic pluck reverses chain + field mutations."""
    s = Session.load(simple_chain)
    pre = json.loads(json.dumps(s.records))
    s.pluck_records([3])
    s.undo_last()
    for i, (before, after) in enumerate(zip(pre, s.records)):
        assert before == after, f"record {i} not restored after holistic pluck undo"


def test_break_sibling_merge_is_aliased_to_pluck(simple_chain: Path) -> None:
    """break_sibling_merge is now an alias for pluck_records; verifies it
    creates a single 'pluck' op (not 'mutate') and applies the full unlink."""
    s = Session.load(simple_chain)
    a2 = s.records[3]
    original_msg_id = a2["message"]["id"]
    s.break_sibling_merge(3)
    # Should have created exactly one 'pluck' op (not 'mutate')
    ops = s._pending_ops
    assert len(ops) == 1
    assert ops[0]["type"] == "pluck"
    # And it should have applied the holistic effects
    assert a2["message"]["id"] != original_msg_id
    assert a2["parentUuid"].startswith("00000000-inspector-unlinked-")
    assert PLUCK_FIELD in a2


def test_quick_action_response_no_op_does_not_include_prior_mutations(tmp_path: Path) -> None:
    """Regression: idempotent re-run of a quick action must NOT return the
    prior bulk_mutate's affected indices. Earlier behavior unconditionally
    pulled from s._pending_ops[-1], so a no-op second run flooded the
    frontend with stale DOM updates and hung the browser. The fix gates
    that on result['n_mutated'] > 0."""
    from flask import Flask
    from jsonl_inspect.server import _quick_action_response
    test_app = Flask(__name__)

    # Build a session with enough thinking-block records to make the first
    # bulk_mutate noticeably large; the test value is in the count delta,
    # not the absolute number.
    records = [_make_record("u1", "user", None, content=[{"type": "text", "text": "go"}])]
    prev = "u1"
    for i in range(50):
        a_uuid = f"a{i:03d}"
        records.append(_make_record(
            a_uuid, "assistant", prev,
            msg_id=f"msg_{i:03d}",
            content=[
                {"type": "thinking", "thinking": f"think{i}", "signature": "S" * 100},
                {"type": "text", "text": f"reply{i}"},
            ],
        ))
        u_uuid = f"u{i:03d}n"
        records.append(_make_record(u_uuid, "user", a_uuid,
                                    content=[{"type": "text", "text": f"q{i}"}]))
        prev = u_uuid

    p = tmp_path / "many_thinking.jsonl"
    write_session(p, records)
    s = Session.load(p)

    with test_app.app_context():
        # First run: produces a bulk_mutate op with many mutations.
        result1 = s.strip_thinking_blocks(keep_last_n=1)
        resp1 = _quick_action_response(s, result1)
        body1 = resp1.get_json()
        assert result1["n_mutated"] > 10
        n_affected_run1 = len(body1["affected_summaries"])
        assert n_affected_run1 > 10, f"first run should affect many summaries, got {n_affected_run1}"

        # Second run: idempotent no-op. MUST NOT echo the first run's affected list.
        result2 = s.strip_thinking_blocks(keep_last_n=1)
        resp2 = _quick_action_response(s, result2)
        body2 = resp2.get_json()
        assert result2["n_mutated"] == 0
        n_affected_run2 = len(body2["affected_summaries"])
        # Only preserved_indices should remain — at most keep_last_n entries.
        assert n_affected_run2 <= 1, (
            f"no-op should return at most preserved indices ({1} for keep_last_n=1), "
            f"got {n_affected_run2}. Likely regression of the no-op-pulls-prior-ops bug."
        )


def test_pluck_skips_already_plucked(simple_chain: Path) -> None:
    """Tolerant: plucking an already-plucked record is a no-op, doesn't
    corrupt prior metadata."""
    s = Session.load(simple_chain)
    s.pluck_records([3])
    first_marker = json.loads(json.dumps(s.records[3][PLUCK_FIELD]))
    s.pluck_records([3])  # second pluck on the same record
    second_marker = s.records[3][PLUCK_FIELD]
    assert first_marker == second_marker, "second pluck should not clobber the marker"


def test_offchain_sibling_pulled_into_wire(tmp_path: Path) -> None:
    """Two records sharing message.id: an off-chain thinking record and an
    in-chain text record. The wire-payload computation should include both
    (matching Claude Code's the message.id sibling-merge behavior)."""
    records = [
        _make_record("u1", "user", None, content=[{"type": "text", "text": "go"}]),
        # Off-chain thinking record sharing msg_id with the in-chain text record
        _make_record("a1_thinking", "assistant", "u1",
                     msg_id="msg_a1",
                     content=[{"type": "thinking", "thinking": "X", "signature": "S"}]),
        # In-chain text record (its parent skips the thinking record)
        _make_record("a1_text", "assistant", "u1",
                     msg_id="msg_a1",
                     content=[{"type": "text", "text": "response"}]),
    ]
    p = tmp_path / "sibling_merge.jsonl"
    write_session(p, records)
    s = Session.load(p)

    # a1_thinking is NOT chain-reachable (a1_text's parent is u1, not a1_thinking),
    # but should still contribute to wire bytes via message.id reattach.
    from jsonl_inspect.parser import _compute_wire_payload, _compute_chain_reachable
    reachable = _compute_chain_reachable(s.records)
    assert 1 not in reachable  # a1_thinking is off-chain
    wire = _compute_wire_payload(s.records)
    assert 1 in wire["included_indices"], "off-chain sibling should be pulled in via msg.id merge"


# ---------------------------------------------------------------------------
# bulk-pluck marker-size regression (the 330MB balloon)


def _build_linear_pluckable_chain(tmp_path: Path, n_pairs: int) -> Path:
    """A chain of n_pairs tool exchanges: for each i, an assistant tool_use
    record followed by a user tool_result record. All are pluckable, and
    plucking them re-threads the following record's parentUuid — exercising
    the rethread bookkeeping at scale."""
    records = [
        _make_record("root", "user", None, content=[{"type": "text", "text": "start"}]),
    ]
    prev = "root"
    for i in range(n_pairs):
        au = f"a{i}"
        uu = f"u{i}"
        records.append(_make_record(
            au, "assistant", prev, msg_id=f"msg_a{i}",
            content=[{"type": "tool_use", "id": f"tu{i}", "name": "Read",
                      "input": {"file_path": f"/f{i}.txt"}}],
        ))
        records.append(_make_record(
            uu, "user", au,
            content=[{"type": "tool_result", "tool_use_id": f"tu{i}", "content": "y" * 800}],
        ))
        prev = uu
    # A final live record so the chain tip is past the pluck set.
    records.append(_make_record("tip", "user", prev, content=[{"type": "text", "text": "end"}]))
    p = tmp_path / f"pluckable_{n_pairs}.jsonl"
    write_session(p, records)
    return p


def _max_marker_bytes(s: Session) -> int:
    biggest = 0
    for rec in s.records:
        marker = rec.get(PLUCK_FIELD)
        if isinstance(marker, dict):
            biggest = max(biggest, len(json.dumps(marker, ensure_ascii=False)))
    return biggest


def test_bulk_pluck_marker_size_stays_flat(tmp_path: Path) -> None:
    """Per-record pluck markers must NOT grow with batch size. The old form
    wrote the whole batch's plucked_uuids + rethreads into every record's
    marker -> O(N^2) -> a real 1824-record pluck ballooned a session to 330MB.
    Marker size for a 20-record batch and a 400-record batch should be within
    a small constant of each other."""
    small = Session.load(_build_linear_pluckable_chain(tmp_path, 20))
    small.pluck_records([i for i in range(1, len(small.records) - 1)])
    small_marker = _max_marker_bytes(small)

    big = Session.load(_build_linear_pluckable_chain(tmp_path, 400))
    big.pluck_records([i for i in range(1, len(big.records) - 1)])
    big_marker = _max_marker_bytes(big)

    # 20x the batch must not meaningfully grow the per-record marker.
    assert big_marker < small_marker + 512, (
        f"marker grew with batch size: {small_marker}B (20) -> {big_marker}B (400)"
    )
    # And the absolute size must be tiny.
    assert big_marker < 2048, f"marker unexpectedly large: {big_marker}B"


def test_bulk_pluck_file_size_is_linear(tmp_path: Path) -> None:
    """Total on-disk size after a bulk pluck should scale ~linearly with the
    batch, not quadratically."""
    def plucked_file_bytes(n_pairs: int) -> int:
        p = _build_linear_pluckable_chain(tmp_path, n_pairs)
        s = Session.load(p)
        s.pluck_records([i for i in range(1, len(s.records) - 1)])
        s.save()
        return p.stat().st_size

    b100 = plucked_file_bytes(100)
    b400 = plucked_file_bytes(400)
    # 4x the records -> at most ~5x the bytes (linear + slack). The O(N^2)
    # form would have produced ~16x.
    assert b400 < b100 * 6, f"file grew super-linearly: {b100}B (100) -> {b400}B (400)"


def test_bulk_pluck_unpluck_round_trip(tmp_path: Path) -> None:
    """A bulk pluck must fully reverse via unpluck (batch_id grouping),
    restoring every re-threaded parentUuid and removing every marker."""
    p = _build_linear_pluckable_chain(tmp_path, 30)
    original = [dict(r) for r in Session.load(p).records]
    original_parents = {r["uuid"]: r["parentUuid"] for r in original}

    s = Session.load(p)
    idxs = [i for i in range(1, len(s.records) - 1)]
    s.pluck_records(idxs)
    # Unpluck from any single member — batch_id should reverse the whole group.
    s.unpluck_record(idxs[0])

    for rec in s.records:
        assert PLUCK_FIELD not in rec, f"marker survived on {rec.get('uuid')}"
        assert rec["parentUuid"] == original_parents[rec["uuid"]], (
            f"parentUuid not restored on {rec.get('uuid')}"
        )


def test_legacy_plucked_uuids_marker_still_unplucks(tmp_path: Path) -> None:
    """Files plucked before the batch_id fix carry the legacy marker format
    (whole-batch plucked_uuids + rethreads on every record). _do_unpluck must
    still reverse them."""
    records = [
        _make_record("u1", "user", None, content=[{"type": "text", "text": "hi"}]),
        _make_record("a1", "assistant", "u1", msg_id="msg_a1",
                     content=[{"type": "text", "text": "hello"}]),
        _make_record("u2", "user", "a1", content=[{"type": "text", "text": "next"}]),
    ]
    # Hand-craft a legacy pluck of a1: u2 re-threaded from a1 -> u1, and a1
    # carries a legacy marker.
    records[2]["parentUuid"] = "u1"
    records[1][PLUCK_FIELD] = {
        "plucked_uuids": ["a1"],
        "rethreads": [{"child_uuid": "u2", "old_parent_uuid": "a1"}],
    }
    p = tmp_path / "legacy_marker.jsonl"
    write_session(p, records)

    s = Session.load(p)
    s.unpluck_record(1)
    assert PLUCK_FIELD not in s.records[1], "legacy marker not removed"
    assert s.records[2]["parentUuid"] == "a1", "legacy rethread not restored"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# trim-tool-calls quick action


def test_trim_tool_calls_trims_bulk_keeps_structure(chain_with_tool_calls: Path) -> None:
    """Bulky tool_use inputs and tool_result content get the placeholder; the
    blocks themselves (and their pairing ids) stay intact."""
    s = Session.load(chain_with_tool_calls)
    res = s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    assert res["n_mutated"] >= 2, res
    assert res["bytes_freed_est"] > 2000

    # Structure survives: every tool_use still has an id, every tool_result
    # still points at one.
    tu_ids, tr_fors = set(), set()
    for summ in s.summaries:
        for b in summ.blocks:
            if b.type == "tool_use" and b.tool_use_id:
                tu_ids.add(b.tool_use_id)
            if b.type == "tool_result" and b.tool_result_for:
                tr_fors.add(b.tool_result_for)
    assert tu_ids, "tool_use blocks should still exist after trim"
    assert tr_fors <= tu_ids, "every tool_result should still reference a live tool_use"

    # And the placeholder is actually in there.
    blob = json.dumps([r.get("message") for r in s.records], ensure_ascii=False)
    assert TRIM_PLACEHOLDER in blob


def test_trim_tool_calls_preserves_small_fields(chain_with_tool_calls: Path) -> None:
    """Short values (file paths, etc.) fall under the threshold and pass through."""
    s = Session.load(chain_with_tool_calls)
    s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    blob = json.dumps([r.get("message") for r in s.records], ensure_ascii=False)
    assert "/short/path.txt" in blob, "small file_path should survive trimming"


def test_trim_tool_calls_idempotent(chain_with_tool_calls: Path) -> None:
    s = Session.load(chain_with_tool_calls)
    first = s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    assert first["n_mutated"] > 0
    second = s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    assert second["n_mutated"] == 0, "re-run should find nothing new to trim"


def test_prune_sequence_strip_then_trim(tmp_path: Path) -> None:
    """The combined 'prune' path: strip thinking, then trim tool content.
    Both apply, and the tool structure is still present afterward."""
    big = "y" * 900
    records = [
        _make_record("u0", "user", None, content=[{"type": "text", "text": "start"}]),
        _make_record("a0", "assistant", "u0", msg_id="m0",
                     content=[{"type": "thinking", "thinking": "hmm", "signature": "S0"}]),
        _make_record("a0t", "assistant", "a0", msg_id="m0b",
                     content=[{"type": "tool_use", "id": "t0", "name": "Read",
                               "input": {"file_path": "/a.txt", "content": big}}]),
        _make_record("u1", "user", "a0t",
                     content=[{"type": "tool_result", "tool_use_id": "t0", "content": big}]),
        _make_record("a1", "assistant", "u1", msg_id="m1",
                     content=[{"type": "thinking", "thinking": "more", "signature": "S1"},
                              {"type": "text", "text": "done"}]),
        _make_record("tip", "user", "a1", content=[{"type": "text", "text": "end"}]),
    ]
    p = tmp_path / "prune.jsonl"
    write_session(p, records)
    s = Session.load(p)
    strip = s.strip_thinking_blocks(keep_last_n=0)
    trim = s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    assert strip["n_mutated"] >= 1
    assert trim["n_mutated"] >= 1
    s.save()

    s2 = Session.load(p)
    # No thinking blocks left anywhere on the chain.
    for rec in s2.records:
        msg = rec.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            assert not any(
                isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
                for b in msg["content"]
            ), "thinking survived the strip"
    # Tool structure intact.
    has_tool_use = any(
        b.type == "tool_use" for summ in s2.summaries for b in summ.blocks
    )
    assert has_tool_use, "trim must keep the tool_use block itself"


# ---------------------------------------------------------------------------
# live-session guard (editing a session that's still open in Claude Code)


def test_check_live_quiet_file(simple_chain: Path) -> None:
    """A file nobody else is touching should read as not-live."""
    s = Session.load(simple_chain)
    live = s.check_live()
    assert live["is_live"] is False
    assert live["file_changed"] is False


def test_check_live_detects_external_append(simple_chain: Path) -> None:
    """An external append (what Claude Code does to a running session) must
    be detected."""
    s = Session.load(simple_chain)
    with simple_chain.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "user", "uuid": "appended", "parentUuid": None,
            "message": {"role": "user", "content": [{"type": "text", "text": "live"}]},
        }) + "\n")
    live = s.check_live()
    assert live["is_live"] is True
    assert live["file_changed"] is True
    assert "changed on disk" in live["detail"]


def test_save_blocks_on_live_session(simple_chain: Path) -> None:
    """The important one: a save into a live session must refuse rather than
    clobber it. Pending changes survive the refusal."""
    s = Session.load(simple_chain)
    s.strip_thinking_blocks(keep_last_n=0)
    assert len(s._pending_ops) > 0

    with simple_chain.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "user", "uuid": "appended2", "parentUuid": None,
            "message": {"role": "user", "content": [{"type": "text", "text": "live"}]},
        }) + "\n")

    with pytest.raises(SessionLiveError) as excinfo:
        s.save()
    assert "open in Claude Code" in str(excinfo.value)
    assert excinfo.value.detail.get("is_live") is True
    # Nothing was lost — the user can close the session and retry.
    assert len(s._pending_ops) > 0


def test_save_force_overrides_live_guard(simple_chain: Path) -> None:
    """force=True is the deliberate override."""
    s = Session.load(simple_chain)
    s.strip_thinking_blocks(keep_last_n=0)
    with simple_chain.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "user", "uuid": "appended3", "parentUuid": None,
            "message": {"role": "user", "content": []},
        }) + "\n")
    result = s.save(force=True)
    assert result["ops_saved"] > 0


def test_own_save_does_not_trip_the_guard(simple_chain: Path) -> None:
    """Our own write must re-baseline, or the second save would falsely
    report the session as live."""
    s = Session.load(simple_chain)
    s.strip_thinking_blocks(keep_last_n=1)
    s.save()
    assert s.check_live()["is_live"] is False, "our own save tripped the guard"
    # A second round must also go through.
    s.mutate_field(0, "message.content", [{"type": "text", "text": "edited"}])
    result = s.save()
    assert result["ops_saved"] > 0


def test_save_reports_wire_before_and_after(chain_with_tool_calls: Path) -> None:
    """The post-save panel needs an honest before→after: 'before' is the last
    SAVED state, not whatever the size was when save() happened to be called."""
    s = Session.load(chain_with_tool_calls)
    baseline = s.stats.wire_messages_bytes
    s.trim_tool_calls(keep_last_n=0, threshold_bytes=500)
    result = s.save()
    assert result["wire_bytes_before"] == baseline
    assert result["wire_bytes_after"] < baseline, "trim should shrink the wire payload"
