# jsonl-inspect

A browser-based inspector **and surgical editor** for Claude Code session
JSONL files.

Two problems it solves:

1. **Reading** multi-megabyte, single-line JSON transcripts in a code editor is
   miserable. jsonl-inspect renders a session as a scannable feed of cards with
   composition stats, filters, and search.
2. **Reclaiming context.** A long session's real cost lives in its transcript,
   and Claude Code rebuilds each API request's context *from that local file*.
   By editing the file — removing thinking blocks, trimming bulky tool
   content, unlinking records from the chain — you can shrink what gets sent
   on the next request, often dramatically. Real result: a session at **86%
   context recovered to 16%** in a couple of clicks.

Everything is reversible, every save takes a timestamped backup, and the whole
thing runs locally against files you already own.

> **Scope note.** This operates on *your own* local session files. It's a
> personal tool for inspection, redaction, and context management — not a way
> to defeat anything server-side. The context math works because Claude Code is
> local-first and treats the transcript as the source of truth.

## Quick start

```bash
uv sync                                        # one-time setup
uv run jsonl-inspect                           # opens the session picker
uv run jsonl-inspect /path/to/session.jsonl    # opens straight into a session
```

The picker lists every Claude Code project (decoded to real paths) and the
sessions inside each, with titles, sizes, and ages. Pick one and it loads.

> **Never run it against a session that's currently open in Claude Code.** The
> live process writes to the file; a concurrent write from here will corrupt
> it. Close the session first.

## The mental model (why editing the file changes anything)

Claude Code reconstructs each request's `messages` array from the JSONL at
send time. It does **not** send the whole file — it runs a pipeline (observed
behaviour; details and evidence in `docs/DESIGN-NOTES.md`):

1. **Chain walk** — from the newest record, follow `parentUuid` backward to the
   root. Only records on this chain are candidates.
2. **message.id sibling merge** — assistant records sharing a `message.id` get
   their content concatenated into one wire message (this is how a split
   thinking/text turn rejoins, and how an *off-chain* sibling can still ride
   along).
3. **tool_result reattach** — off-chain user records carrying a `tool_result`
   whose `parentUuid` matches an in-chain record get pulled back in.
4. **strip** — local-only bookkeeping (`parentUuid`, `isSidechain`, and any
   `_inspector_*` fields) is dropped; only `{role, content}` goes on the wire.

So the size that matters is **wire-payload bytes**, not file bytes. The top bar
shows both. Two consequences the tool leans on:

- **Thinking blocks are expensive on the wire** (big cryptographic signatures),
  so stripping them from historical turns is the single biggest lever.
- To actually remove a record from the wire you must defeat *all* the reattach
  mechanisms above — plain chain-unlinking isn't enough for tool_results or
  msg.id-siblings. The **pluck** operation does this holistically.

There's also a **preflight** wrinkle: Claude Code's context-usage gauge reads a
*cached* `usage.input_tokens` from the last assistant response, not a fresh
measurement. After you shrink the wire, the gauge can stay stale-high and block
new requests. The top bar flags this (⚠), and `refresh preflight usage` fixes
it.

## Operations

### Inspect
- Card feed with type tags, sizes, timestamps, previews; expand for full block
  content or raw JSON.
- **Composition** bars, **filter by record type** and **by block type**,
  full-text **search**, **top-by-size**, **compaction events**, **health**.
- **Wire vs file** byte counts and live context %, with the stale-preflight
  warning.

### Edit
- Per-record JSON editor (CodeMirror) with live validation warnings (orphaned
  `tool_use_id`s, broken parent chains, etc.). Lenient by design — it warns,
  it doesn't block.

### Pluck (holistic unlink)
Removes a record from the wire by defeating every reattach mechanism at once:
re-threads its children's `parentUuid`, mutates its own `message.id` and
`parentUuid`, and marks it. Pairing-aware — plucking one half of a
tool_use↔tool_result exchange pulls in the other. Plucking a `compact_boundary`
additionally defuses the load-time prune that would otherwise drop everything
before the boundary, so the pre-compaction tail comes back. Multi-select for
bulk plucks.

### Quick actions (sidebar)
| Action | What it does |
|---|---|
| **✦ prune** | strip-thinking + trim-tool-calls in one click — the two most-used reclamation steps together |
| **strip thinking blocks** | removes `thinking`/`redacted_thinking` from chain-reachable assistant records (keeps last N) |
| **trim tool calls** | replaces bulky tool_use inputs / tool_result content with a short placeholder, keeping the call itself (keeps last N intact) |
| **refresh preflight usage** | resets `usage.input_tokens` on the latest assistant so a shrunk session isn't blocked by the stale gauge |

> **Why trim rather than remove the tool exchange outright?** Removing it
> reclaims a bit more, but it leaves a gap where a call used to be — and
> narration like "let me read that file" can survive with nothing after it. The
> suspected failure there is *quiet*: a model that pattern-matches on
> narrate-then-continue and confidently summarises a call it never made.
> Trimming keeps the call visible and fails *loudly* instead: a repeated
> placeholder can show up in a later tool argument, which looks wrong
> immediately. Given the choice, this ships the mess you can see. See
> `docs/DESIGN-NOTES.md` for the full trade-off.

### Reattach surgery (advanced)
Cards whose wire-inclusion isn't via plain chain-walk get a badge (`↔` sibling
merge, `↰` tool_result reattach). Click it to break that specific mechanism.

### Backups & restore
Every save writes a timestamped `.backup-*.jsonl` alongside the file. The
backups browser lists them (newest first) and restores any with one click
(itself reversible).

## Reversibility model

- **Pending ops** — edits/plucks stage in memory; **save** flushes atomically
  (temp file → `os.replace`) + backup, **discard** reverts to last-saved,
  **undo** reverses the last op.
- **Mutations** store the original value inline (`_inspector_mutations`), so any
  field edit round-trips.
- **Pluck markers** are per-record and grouped by a `batch_id` (each carries
  only its own re-thread info; unpluck reconstructs the batch by id). This is
  what keeps a bulk pluck O(N) on disk instead of O(N²) — an earlier form
  duplicated the whole batch's metadata into every record and ballooned a real
  session to 330MB.
- All `_inspector_*` fields are top-level and local-only — stripped before the
  wire, invisible to Claude Code.

## Layout

- `jsonl_inspect/parser.py` — JSONL loading, per-record analysis, wire-payload
  simulation, all the edit/pluck/mutation/quick-action logic
- `jsonl_inspect/server.py` — Flask app, console entry point, API endpoints
- `static/` — frontend (HTML/CSS/vanilla JS)
- `static/vendor/codemirror.js` — bundled CodeMirror 6 (see `vendor-src/`)
- `tests/` — pytest suite (chain-edit round-trips, bulk-pluck size guarantees)
- `docs/DESIGN-NOTES.md` — why the tool works the way it does: how Claude Code
  assembles a request from the transcript, and what that implies for editing it

## Testing

```bash
uv sync --extra dev
uv run pytest tests/
```

## Regenerating the CodeMirror bundle

```bash
cd vendor-src
rm -rf node_modules package.json package-lock.json
npm init -y > /dev/null
npm install --no-save codemirror @codemirror/lang-json @codemirror/theme-one-dark
npx esbuild --bundle codemirror-entry.mjs --format=esm \
    --outfile=../static/vendor/codemirror.js --minify
```

Commit `static/vendor/codemirror.js`. `vendor-src/node_modules` and
`package-lock.json` are gitignored.

**Don't** add `@codemirror/state` to the install list — it's a transitive dep
of the others, and pinning it separately produces two copies in the bundle
(and the runtime "multiple instances of @codemirror/state" error).
