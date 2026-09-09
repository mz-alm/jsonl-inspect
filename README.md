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
> it. Close the session first. (Both the UI and the CLI detect this and refuse
> to save, but don't rely on it.)

The picker hides **agent sessions** by default. Claude Code writes a separate
`.jsonl` for every Task sub-agent and background job, and they outnumber real
sessions by roughly 70 to 1 — one project here listed 2,440 sessions when 46
were human-driven. A toggle in the picker brings them back.

## Putting it on your PATH

To drop the `cd` and `uv run` gymnastics, symlink the launcher:

```bash
ln -s ~/personal/jsonl-inspect/bin/jsonl-inspect ~/.local/bin/jsonl-inspect
jsonl-inspect --list          # now works from any directory
```

The launcher finds the repo from its own path (through symlinks), so the link
can live anywhere. It runs against the working tree, so a `git pull` needs no
reinstall. If the repo has a virtualenv it uses that interpreter, so the same
command also serves the web UI; without one, every headless action still works
because the CLI imports no third-party packages — `cli.py`, `discovery.py` and
`parser.py` are pure standard library, and only the Flask server is loaded (on
demand) when you actually ask for a browser.

The packaged alternative, if you'd rather have a copy independent of the
checkout:

```bash
uv tool install --editable /path/to/jsonl-inspect
```

### Or build a single file

```bash
./build.sh                  # -> dist/jsonl-inspect
./build.sh ~/.local/bin     # build straight onto your PATH
```

That produces a **~125 KB executable** — one file, no virtualenv, no install
step, nothing unpacked at runtime, ~60 ms cold start. It's a
[zipapp](https://docs.python.org/3/library/zipapp.html), which is only possible
because the headless CLI has no third-party imports.

It contains the headless actions, not the web UI (that needs Flask, which would
defeat the point). And it isn't a fully static binary — it still needs *a*
`python3` on the machine. That trade is deliberate: embedding the interpreter
costs 10–25 MB and a much slower start to remove a dependency that macOS and
every mainstream Linux already ship.

## Without a browser

Opening a web UI to run a bulk operation breaks the flow of *close the session,
clean it, get back in*. The same actions are available headlessly:

```bash
jsonl-inspect --prune                  # the session you just closed, in this directory
jsonl-inspect --prune --dry-run        # show what would change, write nothing
jsonl-inspect --prune my-session       # by session title, searched across all projects
jsonl-inspect --stats my-session       # composition + wire size, changes nothing
jsonl-inspect --list                   # every interactive session, all projects
jsonl-inspect --strip-thinking --strip-images path/to/session.jsonl
jsonl-inspect --prune --json           # machine-readable
```

With an action and no target, it picks the newest interactive session in the
current directory's project — which is what you just closed. Paths and titles
both work as targets; an ambiguous title lists the candidates and stops rather
than guessing, since these actions write.

Tuning: `--keep-thinking N`, `--keep-tools N`, `--keep-images N`,
`--threshold BYTES`, `--trim-images`. Safety: writes are refused if the session
looks open (`--force` overrides), `--dry-run` is always allowed, and every save
takes a timestamped backup and prints its path.

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

Four layers, and only the last one needs a dependency:

- `jsonl_inspect/parser.py` — a session once loaded: per-record analysis,
  wire-payload simulation, all the edit/mutation/quick-action logic *(stdlib)*
- `jsonl_inspect/discovery.py` — finding sessions on disk: project decoding,
  title extraction, the agent/SDK filter, target resolution *(stdlib)*
- `jsonl_inspect/cli.py` — headless front-end and the console entry point;
  imports the server lazily *(stdlib)*
- `jsonl_inspect/server.py` — the Flask app and API endpoints *(needs Flask)*
- `bin/jsonl-inspect` — PATH launcher, `build.sh` — single-file build (see above)
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
