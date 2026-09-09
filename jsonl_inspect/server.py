"""Flask backend for jsonl-inspect.

Two modes:
- Picker mode (no session loaded): serves a browser for ~/.claude/projects/
  directories and their .jsonl sessions. User picks one to load.
- Inspector mode (session loaded): the full inspector UI from prior phases.

The server starts in picker mode if launched with no file argument, or in
inspector mode if a file path is given on the CLI. Selecting a session in
the picker transitions the server (and the page) into inspector mode.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Cheap pre-filter for the session-title scan: matches both Claude Code's
# compact and default JSON formatting (with/without space after the colon).
_TITLE_LINE_PATTERN = re.compile(r'"type":\s*"(?:custom|ai)-title"')

from flask import Flask, abort, jsonify, request, send_from_directory

from .parser import Session, SessionLiveError

DEFAULT_PORT = 5173
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


class InspectorState:
    """Holds the current Session, if any. Endpoints reach in for the live one."""

    def __init__(self, initial_session: Session | None = None) -> None:
        self.session = initial_session

    def has_session(self) -> bool:
        return self.session is not None

    def load(self, path: str | Path) -> Session:
        self.session = Session.load(path)
        return self.session


def _require_session(state: InspectorState) -> Session:
    """Helper: return the loaded session or abort 409 if none is loaded."""
    if not state.has_session():
        abort(409, description="no session loaded — POST /api/load first")
    return state.session  # type: ignore[return-value]


def _decode_project_path(encoded_name: str, project_dir: Path | None = None) -> str:
    """Best-effort decode of Claude Code's project dir name back to the
    original filesystem path.

    Claude Code encodes paths by substituting '/' with '-', which is
    irreversibly ambiguous for paths containing '-' (e.g., 'salvy-dashboard'
    or 'jsonl-inspect' both produce hyphens that look identical to path
    separators in the encoded name).

    Strategy:
    1. Walk the filesystem from root, greedily matching the longest existing
       directory at each step. Handles hyphen-containing path components and
       gives the actual on-disk path.
    2. Fall back to reading `cwd` from a record (less reliable — cwd is stale
       if the file was moved or generated in a different location).
    3. Final fallback: the encoded name itself.
    """
    if encoded_name.startswith("-"):
        tokens = encoded_name[1:].split("-")
        result = _walk_decode(Path("/"), tokens, 0)
        if result is not None:
            return result

    # Fallback: read cwd from records
    if project_dir is not None and project_dir.is_dir():
        for p in project_dir.iterdir():
            if not p.is_file() or p.suffix != ".jsonl" or ".backup-" in p.name:
                continue
            try:
                with p.open("r", encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        if i >= 50:
                            break
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        cwd = rec.get("cwd")
                        if cwd:
                            return cwd
            except OSError:
                continue
            break

    return encoded_name


def _walk_decode(current: Path, tokens: list[str], i: int) -> str | None:
    """Recursively walk the filesystem, finding the longest matching path
    that joins as many remaining tokens into one directory name as possible."""
    if i >= len(tokens):
        return str(current)
    # Try matching the longest possible run of tokens at this level first
    for j in range(len(tokens), i, -1):
        name = "-".join(tokens[i:j])
        candidate = current / name
        if candidate.is_dir():
            result = _walk_decode(candidate, tokens, j)
            if result is not None:
                return result
    return None


def _extract_session_title(path: Path) -> str | None:
    """Scan the whole session file for title records and return the most
    recent one. Forward scan with a cheap regex pre-filter — most lines
    aren't titles and never get JSON-parsed. Reads ~15MB files in tens of
    milliseconds.

    Walking forward and taking the LAST title we see is equivalent to
    walking backward and taking the FIRST — but a one-pass forward read
    is simpler than block-based reverse reading on text files.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            best_custom: str | None = None
            best_ai: str | None = None
            for line in f:
                if not _TITLE_LINE_PATTERN.search(line):
                    continue
                try:
                    rec = json.loads(line.rstrip("\n"))
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t == "custom-title":
                    v = rec.get("customTitle")
                    if v:
                        best_custom = v
                elif t == "ai-title":
                    v = rec.get("aiTitle")
                    if v:
                        best_ai = v
            # Prefer user-set custom-title over auto-generated ai-title.
            # Claude Code emits both side-by-side (ai-title typically follows
            # each custom-title), so "latest record wins" without this prefer
            # would always return the ai-title.
            return best_custom or best_ai
    except OSError:
        return None


def _is_interactive_session(path: Path, limit: int = 300) -> bool:
    """True if this session was driven by a human at a terminal, rather than
    spawned programmatically.

    Claude Code writes a separate .jsonl for every programmatic invocation —
    Task sub-agents, the memory-extraction pass, and other SDK-driven jobs —
    so a projects dir is overwhelmingly machine traffic. Two fields separate
    them, and either one is sufficient:

    - `entrypoint`: "cli" for the interactive TUI, "sdk-cli" for SDK runs.
    - `promptSource`: "typed" for a human keystroke, "sdk" for a generated one.

    Measured over the whole local corpus (8,881 sessions, 7.5s): 123 pass this
    check, 8,758 don't — and all 92 hand-titled sessions pass, i.e. zero false
    negatives against the only ground truth available. `isSidechain` is *not*
    usable here: it marks sub-agent records nested inside a parent transcript
    and is false throughout these standalone files.

    Only the first `limit` records are examined — the fields appear on the
    earliest records of a session, and reading whole files would mean scanning
    ~1.5 GB to answer a listing query.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    return False
                # Cheap substring gate: most lines carry neither field, and
                # JSON-parsing every line of every session is the slow path.
                if '"entrypoint"' not in line and '"promptSource"' not in line:
                    continue
                try:
                    rec = json.loads(line.rstrip("\n"))
                except json.JSONDecodeError:
                    continue
                if rec.get("entrypoint") == "cli":
                    return True
                if rec.get("promptSource") == "typed":
                    return True
    except OSError:
        return False
    return False


def _quick_action_response(s: Session, result: dict[str, Any]) -> object:
    """Standard response shape for /api/quick-actions/* endpoints.

    Includes the raw `result` payload, the affected record summaries (preserved
    + mutated indices) for in-place UI updates, and the new stats snapshot.
    The frontend uses `affected_summaries` to refresh card visuals without
    re-fetching the full records list.

    Idempotent re-runs return n_mutated/n_plucked=0 and produce no new pending
    op. In that case we must NOT pull from `s._pending_ops[-1]` — that's the
    PRIOR op (often a bulk_mutate/pluck with hundreds-to-thousands of entries),
    and including those indices makes the frontend churn through stale DOM
    updates for tens of seconds. The n>0 checks gate that.

    Handles both mutation-based actions (strip-thinking, trim → bulk_mutate op,
    affected = the mutated indices) and pluck-based actions (pluck-tools →
    pluck op, affected = plucked_indices + re-threaded children). The frontend
    applies is_plucked/is_mutated visuals uniformly from the summaries.
    """
    affected = list(set(result.get("preserved_indices", [])))
    did_something = (
        result.get("n_mutated", 0) > 0 or result.get("n_plucked", 0) > 0
    )
    if did_something and s._pending_ops:
        last = s._pending_ops[-1]
        if last.get("type") == "bulk_mutate":
            for entry in last["mutations"]:
                affected.append(entry["idx"])
        elif last.get("type") == "pluck":
            affected.extend(last.get("plucked_indices", []))
            affected.extend(last.get("children_updated", []))
    affected = sorted(set(affected))
    return jsonify({
        "result": result,
        "affected_summaries": [
            dataclasses.asdict(s.summaries[i]) for i in affected
        ],
        "stats": dataclasses.asdict(s.stats),
    })


def create_app(state: InspectorState) -> Flask:
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )

    # --- Frontend ---

    @app.route("/")
    def index() -> object:
        index_html = STATIC_DIR / "index.html"
        if not index_html.is_file():
            return (
                "<h1>jsonl-inspect</h1><p>Frontend not built.</p>", 200,
            )
        return send_from_directory(STATIC_DIR, "index.html")

    # --- API: app state / file metadata ---

    @app.route("/api/file")
    def file_metadata() -> object:
        if not state.has_session():
            return jsonify({"loaded": False})
        s = state.session  # type: ignore[union-attr]
        return jsonify({
            "loaded": True,
            "path": str(s.file_path),
            "filename": s.file_path.name,
            "size_bytes": s.file_path.stat().st_size,
            "total_records": s.stats.total_records,
        })

    # --- API: picker — browse projects ---

    @app.route("/api/browse")
    def browse_projects() -> object:
        if not CLAUDE_PROJECTS_DIR.is_dir():
            return jsonify({"projects": [], "claude_projects_dir": str(CLAUDE_PROJECTS_DIR)})
        # Counting *interactive* sessions rather than files is what makes this
        # list usable: unfiltered, a project shows thousands of entries when a
        # handful are real. The filter is ~0.13 ms/file (about 1.2s over a
        # ~9k-session corpus), so it's affordable on a picker load; the
        # whole-file title scan is not, and stays in the per-project view.
        include_agents = request.args.get("include_agents") == "1"
        projects = []
        for d in CLAUDE_PROJECTS_DIR.iterdir():
            if not d.is_dir():
                continue
            files = [
                p for p in d.iterdir()
                if p.is_file() and p.suffix == ".jsonl" and ".backup-" not in p.name
            ]
            if not files:
                continue
            session_count = sum(1 for p in files if _is_interactive_session(p))
            # A project with no human-driven sessions is pure agent traffic —
            # nothing anyone would open by hand.
            if session_count == 0 and not include_agents:
                continue
            stat = d.stat()
            projects.append({
                "key": d.name,
                "decoded_path": _decode_project_path(d.name, d),
                "session_count": session_count if not include_agents else len(files),
                "file_count": len(files),
                "mtime": stat.st_mtime,
            })
        projects.sort(key=lambda x: x["mtime"], reverse=True)
        return jsonify({"projects": projects, "claude_projects_dir": str(CLAUDE_PROJECTS_DIR)})

    # --- API: picker — list sessions in a project ---

    @app.route("/api/browse/<project_key>")
    def browse_sessions(project_key: str) -> object:
        # Validate the project_key is a direct child of CLAUDE_PROJECTS_DIR
        # (no path traversal).
        project_dir = (CLAUDE_PROJECTS_DIR / project_key).resolve()
        if not project_dir.is_dir() or project_dir.parent != CLAUDE_PROJECTS_DIR.resolve():
            abort(404)
        # Agent/SDK sessions outnumber real ones ~70:1, so they're excluded by
        # default and the title scan is skipped for them entirely (it's the
        # expensive part — a full-file read each).
        include_agents = request.args.get("include_agents") == "1"
        sessions = []
        n_agents = 0
        for p in project_dir.iterdir():
            if not p.is_file() or p.suffix != ".jsonl":
                continue
            # Skip backup files
            if ".backup-" in p.name:
                continue
            interactive = _is_interactive_session(p)
            if not interactive:
                n_agents += 1
                if not include_agents:
                    continue
            stat = p.stat()
            sessions.append({
                "session_id": p.stem,
                "path": str(p),
                "filename": p.name,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "title": _extract_session_title(p) if interactive else None,
                "is_interactive": interactive,
            })
        sessions.sort(key=lambda x: x["mtime"], reverse=True)
        return jsonify({
            "project_key": project_key,
            "decoded_path": _decode_project_path(project_key, project_dir),
            "sessions": sessions,
            "agent_sessions_hidden": 0 if include_agents else n_agents,
        })

    # --- API: picker — load a session ---

    @app.route("/api/load", methods=["POST"])
    def load_session() -> object:
        body = request.get_json(silent=True) or {}
        path = body.get("path")
        if not path:
            return jsonify({"error": "path required"}), 400
        try:
            s = state.load(path)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "loaded": True,
            "path": str(s.file_path),
            "filename": s.file_path.name,
            "size_bytes": s.file_path.stat().st_size,
            "total_records": s.stats.total_records,
        })

    # --- API: unload current session, return to picker ---

    @app.route("/api/unload", methods=["POST"])
    def unload_session() -> object:
        state.session = None
        return jsonify({"loaded": False})

    # --- API: record summaries (lightweight, no raw content) ---

    @app.route("/api/records")
    def records() -> object:
        s = _require_session(state)
        return jsonify({
            "records": [dataclasses.asdict(sm) for sm in s.summaries],
        })

    # --- API: single full record (raw JSON) ---

    @app.route("/api/records/<int:idx>", methods=["GET"])
    def record_detail(idx: int) -> object:
        s = _require_session(state)
        try:
            return jsonify(s.get_record(idx))
        except IndexError:
            abort(404)

    # --- API: bulk pluck ---

    @app.route("/api/bulk-pluck", methods=["POST"])
    def bulk_pluck() -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        indices = body.get("indices")
        if not isinstance(indices, list) or not indices:
            return jsonify({"error": "POST body must include non-empty 'indices' array"}), 400
        try:
            expanded = s.find_pluck_set(indices)
            result = s.pluck_records(expanded)
        except IndexError as e:
            return jsonify({"error": str(e)}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "plucked_indices": result["plucked_indices"],
            "children_updated": result["children_updated"],
            "summaries": [
                dataclasses.asdict(s.summaries[i])
                for i in result["children_updated"]
            ],
            "plucked_summaries": [
                dataclasses.asdict(s.summaries[i])
                for i in result["plucked_indices"]
            ],
            "user_seeds": indices,
            "stats": dataclasses.asdict(s.stats),
        })

    # --- API: pluck preview ---

    @app.route("/api/records/<int:idx>/pluck-preview", methods=["GET"])
    def record_pluck_preview(idx: int) -> object:
        s = _require_session(state)
        try:
            indices = s.find_pluck_set(idx)
        except IndexError:
            abort(404)
        return jsonify({
            "target_index": idx,
            "pluck_indices": indices,
            "additional_count": len(indices) - 1,
            "summaries": [
                dataclasses.asdict(s.summaries[i]) for i in indices
            ],
        })

    # --- API: soft-pluck record(s) ---

    @app.route("/api/records/<int:idx>", methods=["DELETE"])
    def record_pluck(idx: int) -> object:
        s = _require_session(state)
        try:
            pluck_indices = s.find_pluck_set(idx)
            result = s.pluck_records(pluck_indices)
        except IndexError:
            abort(404)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "plucked_indices": result["plucked_indices"],
            "children_updated": result["children_updated"],
            "summaries": [
                dataclasses.asdict(s.summaries[i])
                for i in result["children_updated"]
            ],
            "plucked_summaries": [
                dataclasses.asdict(s.summaries[i])
                for i in result["plucked_indices"]
            ],
            "stats": dataclasses.asdict(s.stats),
        })

    # --- API: un-pluck preview ---

    @app.route("/api/records/<int:idx>/unpluck-preview", methods=["GET"])
    def record_unpluck_preview(idx: int) -> object:
        s = _require_session(state)
        try:
            rec = s.get_record(idx)
        except IndexError:
            abort(404)
        meta = rec.get("_inspector_pluck")
        if not meta:
            return jsonify({"error": "record is not plucked"}), 400
        plucked_uuids = meta.get("plucked_uuids", [])
        uuid_to_idx: dict[str, int] = {
            r["uuid"]: i for i, r in enumerate(s.records) if r.get("uuid")
        }
        indices = sorted(
            uuid_to_idx[u] for u in plucked_uuids if u in uuid_to_idx
        )
        return jsonify({
            "target_index": idx,
            "unpluck_indices": indices,
            "summaries": [
                dataclasses.asdict(s.summaries[i]) for i in indices
            ],
            "rethreads": meta.get("rethreads", []),
        })

    # --- API: un-pluck ---

    @app.route("/api/records/<int:idx>/unpluck", methods=["POST"])
    def record_unpluck(idx: int) -> object:
        s = _require_session(state)
        try:
            result = s.unpluck_record(idx)
        except IndexError:
            abort(404)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "unplucked_indices": result["unplucked_indices"],
            "children_restored": result["children_restored"],
            "summaries": [
                dataclasses.asdict(s.summaries[i])
                for i in result["children_restored"]
            ],
            "unplucked_summaries": [
                dataclasses.asdict(s.summaries[i])
                for i in result["unplucked_indices"]
            ],
            "stats": dataclasses.asdict(s.stats),
        })

    # --- API: edit a record in place ---

    @app.route("/api/records/<int:idx>", methods=["PUT"])
    def record_update(idx: int) -> object:
        s = _require_session(state)
        body = request.get_json(silent=True)
        if body is None:
            raw_text = request.get_data(as_text=True)
            try:
                body = json.loads(raw_text)
            except json.JSONDecodeError as e:
                return jsonify({"error": f"invalid JSON: {e}"}), 400
        # The frontend may send either the raw record (legacy/simple) or a
        # wrapper {"record": {...}, "warnings": [...]}. Detect via the
        # "record" key — a real record always has "type", "uuid" etc., not
        # a "record" sub-object.
        warnings = None
        if isinstance(body, dict) and "record" in body and isinstance(body["record"], dict):
            warnings = body.get("warnings") or None
            body = body["record"]
        try:
            new_summary = s.update_record(idx, body, warnings=warnings)
        except IndexError:
            abort(404)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "summary": dataclasses.asdict(new_summary),
            "stats": dataclasses.asdict(s.stats),
        })

    # --- API: field-level mutation (reversible surgery) ---

    @app.route("/api/records/<int:idx>/mutate", methods=["POST"])
    def record_mutate(idx: int) -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        field_path = body.get("field_path")
        if not isinstance(field_path, str) or not field_path:
            return jsonify({"error": "body must include string 'field_path'"}), 400
        if "new_value" not in body:
            return jsonify({"error": "body must include 'new_value'"}), 400
        new_value = body["new_value"]
        reason = body.get("reason")
        try:
            s.mutate_field(idx, field_path, new_value, reason=reason)
        except IndexError:
            abort(404)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "summary": dataclasses.asdict(s.summaries[idx]),
            "stats": dataclasses.asdict(s.stats),
        })

    @app.route("/api/records/<int:idx>/mutate", methods=["DELETE"])
    def record_unmutate(idx: int) -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        field_path = body.get("field_path")
        if not isinstance(field_path, str) or not field_path:
            return jsonify({"error": "body must include string 'field_path'"}), 400
        try:
            s.unmutate_field(idx, field_path)
        except IndexError:
            abort(404)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "summary": dataclasses.asdict(s.summaries[idx]),
            "stats": dataclasses.asdict(s.stats),
        })

    # --- API: quick-action shortcuts (built on bulk_mutate) ---

    @app.route("/api/quick-actions/strip-thinking", methods=["POST"])
    def quick_action_strip_thinking() -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        keep_last_n = body.get("keep_last_n", 1)
        if not isinstance(keep_last_n, int) or keep_last_n < 0:
            return jsonify({"error": "'keep_last_n' must be a non-negative int"}), 400
        try:
            result = s.strip_thinking_blocks(keep_last_n=keep_last_n)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return _quick_action_response(s, result)

    @app.route("/api/quick-actions/strip-images", methods=["POST"])
    def quick_action_strip_images() -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        keep_last_n = body.get("keep_last_n", 2)
        if not isinstance(keep_last_n, int) or keep_last_n < 0:
            return jsonify({"error": "'keep_last_n' must be a non-negative int"}), 400
        try:
            result = s.strip_images(keep_last_n=keep_last_n)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return _quick_action_response(s, result)

    @app.route("/api/records/<int:idx>/break-sibling-merge", methods=["POST"])
    def record_break_sibling_merge(idx: int) -> object:
        s = _require_session(state)
        try:
            s.break_sibling_merge(idx)
        except IndexError:
            abort(404)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "summary": dataclasses.asdict(s.summaries[idx]),
            "stats": dataclasses.asdict(s.stats),
        })

    @app.route("/api/records/<int:idx>/break-parent-reattach", methods=["POST"])
    def record_break_parent_reattach(idx: int) -> object:
        s = _require_session(state)
        try:
            s.break_parent_reattach(idx)
        except IndexError:
            abort(404)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "summary": dataclasses.asdict(s.summaries[idx]),
            "stats": dataclasses.asdict(s.stats),
        })

    @app.route("/api/quick-actions/refresh-preflight-usage", methods=["POST"])
    def quick_action_refresh_preflight_usage() -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        new_input_tokens = body.get("new_input_tokens")
        if new_input_tokens is not None and (
            not isinstance(new_input_tokens, int) or new_input_tokens < 0
        ):
            return jsonify({
                "error": "'new_input_tokens' must be a non-negative int or omitted"
            }), 400
        try:
            result = s.refresh_preflight_usage(new_input_tokens=new_input_tokens)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        # Single-record mutate uses the "mutate" op type (not bulk_mutate).
        affected = [result["target_idx"]]
        return jsonify({
            "result": result,
            "affected_summaries": [
                dataclasses.asdict(s.summaries[i]) for i in affected
            ],
            "stats": dataclasses.asdict(s.stats),
        })

    @app.route("/api/quick-actions/trim-tool-calls", methods=["POST"])
    def quick_action_trim_tool_calls() -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        keep_last_n = body.get("keep_last_n", 3)
        threshold_bytes = body.get("threshold_bytes", 500)
        trim_images = bool(body.get("trim_images", False))
        if not isinstance(keep_last_n, int) or keep_last_n < 0:
            return jsonify({"error": "'keep_last_n' must be a non-negative int"}), 400
        if not isinstance(threshold_bytes, int) or threshold_bytes < 0:
            return jsonify({"error": "'threshold_bytes' must be a non-negative int"}), 400
        try:
            result = s.trim_tool_calls(
                keep_last_n=keep_last_n,
                threshold_bytes=threshold_bytes,
                trim_images=trim_images,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return _quick_action_response(s, result)

    @app.route("/api/quick-actions/prune", methods=["POST"])
    def quick_action_prune() -> object:
        """Convenience action: strip-thinking + trim-tool-calls in one call.
        The two most-performed reclamation steps. Stages two bulk_mutate ops;
        undo reverses them one at a time.
        """
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        keep_thinking = body.get("keep_last_thinking", 1)
        keep_tools = body.get("keep_last_tools", 3)
        threshold_bytes = body.get("threshold_bytes", 500)
        trim_images = bool(body.get("trim_images", False))
        if not isinstance(keep_thinking, int) or keep_thinking < 0:
            return jsonify({"error": "'keep_last_thinking' must be a non-negative int"}), 400
        if not isinstance(keep_tools, int) or keep_tools < 0:
            return jsonify({"error": "'keep_last_tools' must be a non-negative int"}), 400
        if not isinstance(threshold_bytes, int) or threshold_bytes < 0:
            return jsonify({"error": "'threshold_bytes' must be a non-negative int"}), 400
        try:
            strip_res = s.strip_thinking_blocks(keep_last_n=keep_thinking)
            pluck_res = s.trim_tool_calls(
                keep_last_n=keep_tools,
                threshold_bytes=threshold_bytes,
                trim_images=trim_images,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        # Union the affected indices from BOTH sub-ops so the frontend refreshes
        # every touched card in one pass.
        affected: set[int] = set()
        affected.update(strip_res.get("preserved_indices", []))
        affected.update(pluck_res.get("preserved_indices", []))
        for op in s._pending_ops[-2:]:
            if op.get("type") == "bulk_mutate":
                affected.update(e["idx"] for e in op["mutations"])
            elif op.get("type") == "pluck":
                affected.update(op.get("plucked_indices", []))
                affected.update(op.get("children_updated", []))
        affected_sorted = sorted(i for i in affected if 0 <= i < len(s.summaries))
        return jsonify({
            "result": {
                "reason": "prune_quick_action",
                "thinking": strip_res,
                "tools": pluck_res,
                "n_mutated": (
                    strip_res.get("n_mutated", 0) + pluck_res.get("n_mutated", 0)
                ),
                "n_thinking_stripped": strip_res.get("n_mutated", 0),
                "n_tools_trimmed": pluck_res.get("n_mutated", 0),
                "bytes_freed_est": (
                    strip_res.get("bytes_freed_est", 0)
                    + pluck_res.get("bytes_freed_est", 0)
                ),
            },
            "affected_summaries": [
                dataclasses.asdict(s.summaries[i]) for i in affected_sorted
            ],
            "stats": dataclasses.asdict(s.stats),
        })

    # --- API: whole-session analytical stats ---

    @app.route("/api/stats")
    def stats() -> object:
        s = _require_session(state)
        return jsonify(dataclasses.asdict(s.stats))

    # --- API: backup browsing / restore ---

    @app.route("/api/backups", methods=["GET"])
    def list_backups() -> object:
        s = _require_session(state)
        return jsonify({"backups": s.list_backups()})

    @app.route("/api/restore", methods=["POST"])
    def restore_backup() -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        backup_path = body.get("backup_path")
        if not backup_path:
            return jsonify({"error": "backup_path required"}), 400
        try:
            result = s.restore_backup(backup_path)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "pre_restore_backup": result["pre_restore_backup"],
            "stats": dataclasses.asdict(s.stats),
        })

    # --- API: pending-changes management ---

    @app.route("/api/pending", methods=["GET"])
    def pending_get() -> object:
        s = _require_session(state)
        return jsonify({
            "ops": s.get_pending_ops(),
            "count": len(s._pending_ops),
        })

    @app.route("/api/save", methods=["POST"])
    def save() -> object:
        s = _require_session(state)
        body = request.get_json(silent=True) or {}
        force = bool(body.get("force", False))

        try:
            result = s.save(force=force)
        except SessionLiveError as e:
            # 409 Conflict: the file is being written by someone else. The
            # frontend turns this into an explanation, not a stack trace.
            return jsonify({
                "error": str(e),
                "live": e.detail,
                "code": "session_live",
            }), 409

        return jsonify({
            "ops_saved": result["ops_saved"],
            "backup_path": result.get("backup_path"),
            "wire_bytes_before": result.get("wire_bytes_before"),
            "wire_bytes_after": result.get("wire_bytes_after"),
            "cached_preflight_tokens": s.stats.latest_input_tokens,
            "stats": dataclasses.asdict(s.stats),
        })

    @app.route("/api/live-check", methods=["GET"])
    def live_check() -> object:
        """Is this session file currently open in Claude Code? Polled by the
        frontend to show a persistent warning banner."""
        s = _require_session(state)
        return jsonify(s.check_live())

    @app.route("/api/discard", methods=["POST"])
    def discard() -> object:
        s = _require_session(state)
        result = s.discard_all()
        return jsonify({
            "ops_discarded": result["ops_discarded"],
            "records": [dataclasses.asdict(sm) for sm in s.summaries],
            "stats": dataclasses.asdict(s.stats),
        })

    @app.route("/api/undo", methods=["POST"])
    def undo() -> object:
        s = _require_session(state)
        try:
            result = s.undo_last()
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "undone_op_type": result["undone_op_type"],
            "affected_indices": result["affected_indices"],
            "affected_summaries": [
                dataclasses.asdict(s.summaries[i])
                for i in result["affected_indices"]
                if 0 <= i < len(s.summaries)
            ],
            "stats": dataclasses.asdict(s.stats),
        })

    return app


def _open_browser_when_ready(url: str, delay: float = 0.6) -> None:
    def opener() -> None:
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=opener, daemon=True).start()


# --- Headless CLI ---------------------------------------------------------
#
# The web UI is the right tool for *inspecting* — scanning cards, picking
# records by hand. The quick actions aren't: they're deterministic bulk
# operations with a couple of parameters, and opening a browser to run one
# breaks the flow of "close the session, clean it, get back in". This layer
# exposes them directly. It adds no new logic — it calls the same Session
# methods the endpoints do.


def _encode_project_key(path: Path) -> str:
    """Claude Code's project-dir encoding: every '/' becomes '-'."""
    return str(path).replace("/", "-")


def _interactive_sessions_in(project_dir: Path) -> list[Path]:
    """Human-driven sessions in one project dir, newest first."""
    if not project_dir.is_dir():
        return []
    files = [
        p for p in project_dir.iterdir()
        if p.is_file() and p.suffix == ".jsonl" and ".backup-" not in p.name
        and _is_interactive_session(p)
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _all_interactive_sessions() -> list[tuple[Path, Path]]:
    """(session_path, project_dir) for every interactive session, newest first."""
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return []
    out: list[tuple[Path, Path]] = []
    for d in CLAUDE_PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        for p in _interactive_sessions_in(d):
            out.append((p, d))
    return sorted(out, key=lambda t: t[0].stat().st_mtime, reverse=True)


def _resolve_target(target: str | None) -> Path:
    """Turn a CLI target into a session path.

    Three forms, in order of directness:
      - an existing path       → used as-is
      - omitted                → newest interactive session in the cwd's
                                 project, i.e. "the one I just closed"
      - anything else          → matched against session titles across all
                                 projects

    Raises SystemExit with a useful message rather than guessing: these
    actions write to the file, so an ambiguous name must stop and ask.
    """
    if target:
        p = Path(target).expanduser()
        if p.is_file():
            return p

    if not target:
        project = CLAUDE_PROJECTS_DIR / _encode_project_key(Path.cwd())
        found = _interactive_sessions_in(project)
        if not found:
            raise SystemExit(
                f"No interactive session found for {Path.cwd()}\n"
                f"  (looked in {project})\n"
                f"  Pass a path or a session name, or use --list to see what's available."
            )
        return found[0]

    # Name lookup. Exact (case-insensitive) beats substring, so a session
    # called "friend" is reachable even though three other titles contain it.
    needle = target.lower()
    exact: list[tuple[Path, Path, str]] = []
    partial: list[tuple[Path, Path, str]] = []
    for p, d in _all_interactive_sessions():
        title = _extract_session_title(p)
        if not title:
            continue
        t = title.lower()
        if t == needle:
            exact.append((p, d, title))
        elif needle in t:
            partial.append((p, d, title))

    matches = exact or partial
    if not matches:
        raise SystemExit(
            f"No session titled like {target!r}. Try --list."
        )
    if len(matches) > 1:
        # Show the project and age, not just the id: a session that has been
        # moved between directories leaves a stale copy behind under the same
        # id, so the id alone can't tell two matches apart.
        lines = "\n".join(
            f"  {t[:36]:<38} {p.stat().st_size / 1024 / 1024:>7.1f} MB  "
            f"{_fmt_age(p.stat().st_mtime):>10}  {_decode_project_path(d.name, d)}"
            for p, d, t in matches
        )
        raise SystemExit(
            f"{target!r} matches {len(matches)} sessions — pass a full path to pick one:\n"
            f"{lines}"
        )
    return matches[0][0]


def _cli_list() -> int:
    """Print every interactive session across all projects."""
    rows = _all_interactive_sessions()
    if not rows:
        print("No interactive sessions found.", file=sys.stderr)
        return 1
    print(f"{'title':<44} {'size':>9}  {'age':>10}  project")
    print("-" * 96)
    for p, d in rows:
        title = _extract_session_title(p) or f"<{p.stem[:8]}>"
        size = p.stat().st_size / 1024 / 1024
        age = _fmt_age(p.stat().st_mtime)
        print(f"{title[:44]:<44} {size:>7.1f} MB  {age:>10}  {_decode_project_path(d.name, d)}")
    print(f"\n{len(rows)} interactive session(s).")
    return 0


def _fmt_age(mtime: float) -> str:
    secs = max(0.0, time.time() - mtime)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


def _fmt_bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.2f}MB"


def _cli_run(args: argparse.Namespace) -> int:
    """Run the requested actions headlessly against one session."""
    path = _resolve_target(args.file)
    session = Session.load(path)
    title = _extract_session_title(path) or "<untitled>"

    if not args.json:
        print(f"session : {title}  ({path.name[:8]})")
        print(f"path    : {path}")
        print(f"size    : {_fmt_bytes(session.stats.total_bytes)} on disk, "
              f"{_fmt_bytes(session.stats.wire_messages_bytes)} on the wire "
              f"({session.stats.wire_messages_count} messages)")

    if args.stats:
        return _cli_stats(session, args)

    # The one genuinely destructive mistake: writing a file Claude Code is
    # holding. In practice the CLI flow makes this rare (you closed the
    # session to get here), but rare isn't never.
    # A dry run writes nothing, so it stays available even on a live session —
    # that's precisely when you want to look before closing anything.
    live = {} if args.dry_run else session.check_live()
    if live.get("is_live") and not args.force:
        print(
            f"\nRefusing to write: this session looks open in Claude Code "
            f"({live.get('detail', 'detected')}).\n"
            f"Close it there and re-run, or pass --force.",
            file=sys.stderr,
        )
        return 2

    wire_before = session.stats.wire_messages_bytes
    results: list[tuple[str, dict[str, Any]]] = []

    if args.prune or args.strip_thinking:
        keep = args.keep_thinking if args.prune else args.keep_last
        results.append(("strip-thinking", session.strip_thinking_blocks(keep_last_n=keep)))
    if args.prune or args.trim_tools:
        results.append((
            "trim-tools",
            session.trim_tool_calls(
                keep_last_n=args.keep_tools,
                threshold_bytes=args.threshold,
                trim_images=args.trim_images,
            ),
        ))
    if args.strip_images:
        results.append(("strip-images", session.strip_images(keep_last_n=args.keep_images)))
    if args.refresh_preflight:
        results.append(("refresh-preflight", session.refresh_preflight_usage()))

    if not results:
        print("\nNothing to do — pass an action (--prune, --stats, …) or --help.",
              file=sys.stderr)
        return 1

    wire_after = session.stats.wire_messages_bytes
    freed = max(0, wire_before - wire_after)
    payload: dict[str, Any] = {
        "session": {"title": title, "path": str(path), "id": path.stem},
        "dry_run": bool(args.dry_run),
        "actions": {name: res for name, res in results},
        "wire_bytes_before": wire_before,
        "wire_bytes_after": wire_after,
        "wire_bytes_freed": freed,
    }

    if args.dry_run:
        session.discard_all()
        payload["saved"] = False
    else:
        save_res = session.save()
        payload["saved"] = True
        payload["backup"] = save_res.get("backup_path")

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print()
    for name, res in results:
        bits = []
        for key, label in (
            ("n_mutated", "records"), ("n_plucked", "plucked"),
            ("n_images", "images"), ("n_trimmed", "trimmed"),
        ):
            if res.get(key):
                bits.append(f"{res[key]} {label}")
        detail = ", ".join(bits) or "nothing to do"
        print(f"  {name:<18} {detail}")

    pct = (100 * freed / wire_before) if wire_before else 0
    print(f"\nwire {_fmt_bytes(wire_before)} → {_fmt_bytes(wire_after)} "
          f"(−{_fmt_bytes(freed)}, {pct:.0f}% smaller)")

    if args.dry_run:
        print("\nDRY RUN — nothing written. Re-run without --dry-run to apply.")
    elif not payload.get("backup"):
        # save() short-circuits when every action was a no-op (already pruned).
        print("\nnothing to save — this session was already clean.")
        return 0
    else:
        print(f"saved · backup: {payload.get('backup')}")
        # Same caveat the web UI's post-save panel gives: the statusline reads
        # a cached number and won't move until the next request.
        print("\nClaude Code's context % won't drop until you send one message "
              "in the session.")
    return 0


def _cli_stats(session: Session, args: argparse.Namespace) -> int:
    st = session.stats
    if args.json:
        print(json.dumps(dataclasses.asdict(st), indent=2, default=str))
        return 0
    print(f"records : {st.wire_messages_count} on the wire / {st.total_records} total")
    print(f"preflight: {st.latest_input_tokens:,} tokens (cached from the last response)"
          if st.latest_input_tokens else "preflight: unknown")
    print("\ncomposition (block types, by wire-relevant bytes):")
    total = sum(st.bytes_by_block_type.values()) or 1
    for btype, b in sorted(st.bytes_by_block_type.items(), key=lambda kv: -kv[1]):
        print(f"  {btype:<14} {_fmt_bytes(b):>10}  {100 * b / total:>5.1f}%")
    print("\nlargest records:")
    # top_records_by_size is [(index, size)]; the readable detail lives on the
    # corresponding summary.
    for idx, size in st.top_records_by_size[:8]:
        summ = session.summaries[idx] if 0 <= idx < len(session.summaries) else None
        rtype = summ.type if summ else "?"
        preview = ((summ.preview or "") if summ else "").replace("\n", " ")
        print(f"  idx {idx:<7} {_fmt_bytes(size):>10}  {rtype:<11} {preview[:44]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jsonl-inspect",
        description="Browser-based inspector for Claude Code session JSONL files.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help=(
            "Session to act on: a path, or a session title (matched across all "
            "projects). With a CLI action and no target, the newest interactive "
            "session in the current directory's project is used. With no action, "
            "a path opens the inspector and omitting it shows the picker."
        ),
    )

    # Headless actions. Flags rather than subcommands so the existing
    # `jsonl-inspect [path]` usage keeps working and actions compose.
    cli = parser.add_argument_group("headless actions (no browser)")
    cli.add_argument("--prune", action="store_true",
                     help="strip thinking + trim tool content (the usual pair)")
    cli.add_argument("--strip-thinking", action="store_true",
                     help="remove thinking blocks from chain-reachable assistants")
    cli.add_argument("--trim-tools", action="store_true",
                     help="replace bulky tool_use inputs / tool_result content")
    cli.add_argument("--strip-images", action="store_true",
                     help="remove images pasted into a turn")
    cli.add_argument("--refresh-preflight", action="store_true",
                     help="reset usage.input_tokens on the latest in-chain assistant")
    cli.add_argument("--stats", action="store_true",
                     help="print composition and wire size, change nothing")
    cli.add_argument("--list", action="store_true", dest="list_sessions",
                     help="list interactive sessions across all projects")

    opts = parser.add_argument_group("headless options")
    opts.add_argument("--dry-run", action="store_true",
                      help="show what would change, write nothing")
    opts.add_argument("--json", action="store_true",
                      help="machine-readable output")
    opts.add_argument("--force", action="store_true",
                      help="write even if the session looks open in Claude Code")
    opts.add_argument("--keep-thinking", type=int, default=1, metavar="N",
                      help="thinking-bearing turns to leave intact (default: 1)")
    opts.add_argument("--keep-tools", type=int, default=3, metavar="N",
                      help="tool exchanges to leave intact (default: 3)")
    opts.add_argument("--keep-images", type=int, default=2, metavar="N",
                      help="image-bearing turns to leave intact (default: 2)")
    opts.add_argument("--keep-last", type=int, default=1, metavar="N",
                      help="keep-last for a lone --strip-thinking (default: 1)")
    opts.add_argument("--threshold", type=int, default=500, metavar="BYTES",
                      help="trim tool strings longer than this (default: 500)")
    opts.add_argument("--trim-images", action="store_true",
                      help="also trim images nested inside tool results")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't automatically open the browser",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run Flask in debug mode (auto-reload)",
    )
    args = parser.parse_args(argv)

    # Headless paths return before any Flask machinery starts.
    if args.list_sessions:
        return _cli_list()
    if any((args.prune, args.strip_thinking, args.trim_tools, args.strip_images,
            args.refresh_preflight, args.stats)):
        try:
            return _cli_run(args)
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 1
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    state = InspectorState()

    if args.file:
        try:
            print(f"Loading {args.file}...", file=sys.stderr)
            session = state.load(args.file)
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(
            f"Loaded {session.stats.total_records} records, "
            f"{session.stats.total_bytes:,} bytes total, "
            f"{session.stats.api_bytes:,} bytes API-relevant",
            file=sys.stderr,
        )
    else:
        print("Starting in picker mode (no file argument given)", file=sys.stderr)

    url = f"http://127.0.0.1:{args.port}"
    print(f"Serving on {url} (Ctrl+C to stop)", file=sys.stderr)

    if not args.no_open:
        _open_browser_when_ready(url)

    app = create_app(state)
    app.run(host="127.0.0.1", port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
