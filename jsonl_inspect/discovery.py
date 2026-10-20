"""Finding Claude Code sessions on disk.

Split out of the Flask app so the headless CLI can use it without importing
a web framework. Everything here is standard library, which is what lets
`jsonl-inspect --prune` run on bare system Python with no virtualenv.

Layering: parser.py is about a session once loaded; this is about locating
one; cli.py and server.py are the two front-ends over both.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


def _claude_projects_dir() -> Path:
    """Where Claude Code keeps session transcripts.

    Claude Code moves its whole config dir -- sessions included -- to
    $CLAUDE_CONFIG_DIR when that's set, which is how people run one login
    per account (`CLAUDE_CONFIG_DIR=~/.claude-work claude`). Reading only
    ~/.claude would silently look at the wrong account's sessions.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / "projects"


CLAUDE_PROJECTS_DIR = _claude_projects_dir()

# Cheap pre-filter for the session-title scan: matches both Claude Code's
# compact and default JSON formatting (with/without space after the colon).
_TITLE_LINE_PATTERN = re.compile(r'"type":\s*"(?:custom|ai)-title"')


def _decode_project_path(encoded_name: str, project_dir: Path | None = None) -> str:
    """Best-effort decode of Claude Code's project dir name back to the
    original filesystem path.

    Claude Code encodes paths by substituting '/' with '-', which is
    irreversibly ambiguous for paths containing '-' (e.g., 'my-dashboard'
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


def _encode_project_key(path: Path) -> str:
    """Claude Code's project-dir encoding: every '/' becomes '-'."""
    return str(path).replace("/", "-")


# A session id is a UUID; its first group is 8 hex characters. That group is
# short enough to type, long enough to be unique in practice (five characters
# already separate every session in a ~130-session corpus), and it's the slice
# Claude Code itself shows, so the two agree.
SHORT_ID_LEN = 8
MIN_ID_PREFIX = 4


def _short_id(path: Path) -> str:
    """The handle for a session: the first group of its uuid filename."""
    return path.stem[:SHORT_ID_LEN]


def _looks_like_id(s: str) -> bool:
    """Could this target be a uuid prefix rather than a title?

    Deliberately narrow — hex digits and dashes only, four or more. A title
    made purely of hex ("beef", "add") would be tried as an id first and then
    fall through to the title search, so the guess costs nothing.
    """
    return (
        len(s) >= MIN_ID_PREFIX
        and all(c in "0123456789abcdef-" for c in s.lower())
    )


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


def _candidate_lines(matches: list[tuple[Path, Path, str]]) -> str:
    """Render ambiguous matches: id, label, size, age, project.

    The project is always shown because a session moved between directories
    leaves a stale copy behind under the *same* uuid — in that case even the
    full id can't separate them and only a path will do.
    """
    return "\n".join(
        f"  {_short_id(p)}  {label[:32]:<34} "
        f"{p.stat().st_size / 1024 / 1024:>7.1f} MB  "
        f"{_fmt_age(p.stat().st_mtime):>10}  {_decode_project_path(d.name, d)}"
        for p, d, label in matches
    )


def _resolve_target(target: str | None) -> Path:
    """Turn a CLI target into a session path.

    Four forms, in order of directness:
      - an existing path       → used as-is
      - omitted                → newest interactive session in the cwd's
                                 project, i.e. "the one I just closed"
      - a uuid prefix          → matched against session ids (any unambiguous
                                 prefix of 4+ characters, like a git sha)
      - anything else          → matched against session titles across all
                                 projects

    Raises SystemExit with a useful message rather than guessing: these
    actions write to the file, so an ambiguous target must stop and ask.
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

    sessions = _all_interactive_sessions()

    # Id lookup first — it's the unambiguous handle, and it's cheap (no file
    # reads at all, where the title search opens every session). A prefix that
    # matches nothing falls through to the title search below.
    if _looks_like_id(target):
        needle_id = target.lower()
        found = [(p, d) for p, d in sessions
                 if p.stem.lower().startswith(needle_id)]
        if len(found) == 1:
            return found[0][0]
        if len(found) > 1:
            # Titles cost a full-file read each, so they're only resolved for
            # the handful of candidates — and only once the answer is unclear.
            hits = [(p, d, _extract_session_title(p) or "<untitled>")
                    for p, d in found]
            same_uuid = len({p.stem for p, _, _ in hits}) == 1
            hint = (
                "these are the same session id in two project directories — "
                "pass a path to pick one"
                if same_uuid else
                "use more characters, or pass a path"
            )
            raise SystemExit(
                f"{target!r} matches {len(hits)} sessions — {hint}:\n"
                f"{_candidate_lines(hits)}"
            )

    # Name lookup. Exact (case-insensitive) beats substring, so a session
    # called "friend" is reachable even though three other titles contain it.
    needle = target.lower()
    exact: list[tuple[Path, Path, str]] = []
    partial: list[tuple[Path, Path, str]] = []
    for p, d in sessions:
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
        same_uuid = len({p.stem for p, _, _ in matches}) == 1
        hint = (
            "same session id in two project directories — pass a path to pick one"
            if same_uuid else
            "pass one of the ids on the left instead"
        )
        raise SystemExit(
            f"{target!r} matches {len(matches)} sessions — {hint}:\n"
            f"{_candidate_lines(matches)}"
        )
    return matches[0][0]



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


