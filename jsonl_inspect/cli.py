"""Headless CLI for jsonl-inspect.

The web UI is the right tool for *inspecting* — scanning cards, picking
records by hand. The quick actions aren't: they're deterministic bulk
operations with a couple of parameters, and opening a browser to run one
breaks the flow of "close the session, clean it, get back in".

This module deliberately imports no web framework. Flask is pulled in only
if the invocation actually wants the server (see main()), so the headless
actions work from any directory with nothing installed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from .discovery import (
    CLAUDE_PROJECTS_DIR,
    _all_interactive_sessions,
    _decode_project_path,
    _encode_project_key,
    _extract_session_title,
    _fmt_age,
    _fmt_bytes,
    _interactive_sessions_in,
    _is_interactive_session,
    _resolve_target,
)
from .parser import Session

DEFAULT_PORT = 5173


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

    # Everything below needs the web stack. Imported here, not at module
    # scope, so the headless actions above stay dependency-free — that's what
    # lets the launcher script run on bare system Python.
    try:
        from .server import serve
    except ImportError as e:  # pragma: no cover - depends on install shape
        print(
            f"The web UI needs Flask, which isn't installed ({e}).\n"
            f"Headless actions (--prune, --stats, --list, …) work without it.",
            file=sys.stderr,
        )
        return 1
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
