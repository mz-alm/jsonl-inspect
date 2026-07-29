# Design notes

Why the tool works the way it does. These are empirical findings about how
Claude Code assembles an API request from a session JSONL — established by
capturing real `/v1/messages` requests with a local proxy and comparing them
against the on-disk transcript. If you're wondering "why does the inspector
care about *that*," the answer is usually here.

Everything below is behaviour observed from outside: what goes on the wire for
a given file state. No claims about implementation internals.

## The file is not the payload

Claude Code does **not** send your whole session file. It reconstructs the
`messages` array at send time, and several filters apply. So "this session is
23MB" tells you almost nothing about what a request costs — the number that
matters is the assembled payload, which the inspector calls **wire bytes** and
shows in the top bar next to file bytes.

### 1. Chain reachability decides inclusion

Records form a linked list via `parentUuid`. Claude Code walks backward from the
newest user/assistant record (the "tip") and includes only records reachable
along that chain. Anything unreachable is absent from the payload — whether it
became unreachable through an Esc-Esc rollback fork, a re-threaded `parentUuid`,
or a compact boundary's `parentUuid: null` severance.

Verified by plucking a record from a test session and diffing captures: the
plucked message's text appeared twice in the baseline payload and zero times
afterward. Holds for both fresh and resumed sessions.

### 2. Local bookkeeping is stripped

On the wire each message is exactly `{role, content}`. Everything else in a
record is local-only and never sent: `uuid`, `parentUuid`, `cwd`, `gitBranch`,
`version`, `sessionId`, `timestamp`, `isSidechain`, `requestId`, `userType`,
`type`, `entrypoint`, and the `toolUseResult` wrapper (which duplicates Read
content alongside `message.content`).

Practical consequence: the inspector's own `_inspector_*` fields are top-level
and therefore invisible to Claude Code. Reversibility metadata costs disk, not
context.

### 3. Thinking blocks ARE sent, and they dominate

Worth stating plainly because an earlier version of these notes got it backwards:
historical thinking blocks are **present and billed** on the wire.

Evidence: a resumed 827-message session's capture contained 336 thinking blocks
contributing roughly 611k of ~813k input tokens — about **75%** of the payload.
A controlled two-turn test isolated one block (1,346 chars of text + 6,708 chars
of signature) and measured ~1,929 tokens for the assistant+user pair, versus
~390 predicted if thinking were stripped: a 5× mismatch that rules out
stripping.

The earlier wrong conclusion came from a small test session that simply had no
thinking turns — absence of thinking content read as evidence of stripping. A
good reminder to check that a negative result isn't just an empty fixture.

This is why **stripping historical thinking is the single highest-value
operation** in the tool. It's also clean: thinking blocks aren't tool calls, so
removing them leaves no structural gap and no placeholder behind.

### 4. Records are merged by `message.id`

Inclusion isn't purely chain-based. Assistant records that share a `message.id`
are merged into one wire message, with their content blocks concatenated — and
the merge finds siblings **regardless of whether they're chain-reachable**.

This matters because an extended-thinking turn is written to the JSONL as *two*
records — one holding the thinking block, one holding the text — sharing a
`message.id`. They rejoin on the wire.

Consequences, all verified:

- Unlinking *only* the thinking record does **not** remove its content: the
  still-reachable text sibling pulls it back in via the merge.
- Physically deleting the record does remove it (measured −1,253 tokens for the
  block above).
- **Mutating just the record's `message.id`** also removes it — the record stays
  on disk, but nothing shares its new id, so the merge finds no sibling. This is
  the lightest-touch removal, and it's what the inspector's "break sibling
  merge" badge does.

(An earlier hypothesis blamed file adjacency. It looked right because siblings
are usually written adjacently — adjacency was a proxy for shared `message.id`.
Retracted.)

### 5. Tool results are re-attached by parent

Off-chain user records carrying a `tool_result` are pulled back into the payload
when their `parentUuid` matches a record that *is* on the chain. So chain-
unlinking a tool result isn't sufficient on its own either — hence the
inspector's "break parent reattach" badge, which mutates the `parentUuid` so the
match fails.

### 6. Empty tool content is legal

Emptying a `tool_use.input` or a `tool_result.content` keeps the transcript
loadable and the request valid — verified end-to-end by resuming a doctored
session and getting a normal response. Pairing survives because `tool_use_id`
lives in the block envelope, not the content.

Two caveats learned the hard way:

- Whatever remains must still be a **schema-valid call for that tool**. A `Read`
  with no `file_path`, or with a `content` parameter it doesn't accept, reads as
  malformed — a downstream session flagged exactly that, unprompted, while not
  minding the missing bulk at all. **Malformation is louder than absence.**
- A run of hollow calls is itself a visible pattern ("three empty files in a
  row"). Elision that leaves stubs can be more conspicuous than elision that
  leaves nothing. There's no free lunch; pick which seam you prefer.

## The context gauge lies (sometimes)

Claude Code's context-usage indicator reads a **cached** `usage.input_tokens`
from the most recent assistant response — not a fresh measurement of what the
next request would cost. So after you shrink a session, the gauge can keep
reporting the old, larger number and refuse to send.

This is a closed loop worth understanding, because it looks exactly like "the
tool didn't work":

1. You reclaim context; the real payload is now smaller.
2. Preflight reads the stale cached number and blocks the request.
3. No request fires, so no fresh usage number comes back.
4. The gauge stays stale. Repeat.

The inspector surfaces this — the top bar shows the cached preflight number
alongside the computed wire size and flags a mismatch — and the **refresh
preflight usage** action breaks the loop by rewriting `usage.input_tokens` on
the latest assistant record. The next real response overwrites it with truth.

## Caching

Requests use prompt caching, with the cache breakpoint at the latest user
message. Editing anything in the chain **before** that breakpoint invalidates
the cache, so the first request after a save costs a full cache write. It's a
one-time cost per edit session, not a per-request tax — worth knowing so a
one-off billing spike after surgery doesn't look like a bug.

There's also a fixed per-request overhead (system prompt + tool definitions) on
the order of tens of KB, independent of conversation length. The inspector's
wire estimate deliberately excludes it, since it isn't something you can edit.

## Trim vs. remove: a real trade-off

The tool trims bulky tool content in place rather than deleting tool exchanges
outright. That's a deliberate choice between two imperfect options:

- **Trim (what this does).** Leaves the `tool_use`/`tool_result` structure intact
  with a placeholder where the bulk was. The call is still visibly *there*.
  Downside: a repeated placeholder string can act as a pattern the model
  reproduces — it may show up in a later tool argument. That failure is **loud**:
  it looks wrong, and it breaks in ways you notice immediately.
- **Remove entirely.** No placeholder to reproduce, and it reclaims more bytes.
  Downside: it leaves a gap where a call used to be, and narration like "let me
  read that file" can survive with nothing following it. The suspected failure
  there is **quiet**: a model that pattern-matches on narrate-then-continue and
  produces a confident summary of a call it never made.

Both are plausible; neither is cleanly measurable, because "how likely is the
model to skip a call" is a behavioural propensity and any fixture built to test
it biases the result. Given that, this tool ships the **loud** failure mode on
purpose. A mess you can see beats a convincing one you can't.

## Reversibility

Nothing is destructive by default:

- **Pending ops** — operations stage in memory. **save** writes atomically
  (temp file → rename) and takes a timestamped backup first; **discard** reverts
  to last-saved; **undo** reverses the last op.
- **Mutations** store the original value inline under `_inspector_mutations`,
  keyed by field path, so any edit round-trips.
- **Unlink markers** are per-record and share a `batch_id`; each record carries
  only its own re-thread info, so a bulk operation stays O(N) on disk. (An
  earlier design duplicated the whole batch's metadata onto every record and
  turned a 20MB session into 330MB. Hence the per-record form.)
- Every `_inspector_*` field is top-level, and per finding #2 above, invisible
  to Claude Code.

## Known unknowns

- Whether a run of emptied-but-valid tool calls measurably affects downstream
  behaviour, versus merely being noticeable. Untested; see the trade-off above.
- Whether repeated system-reminder blocks in long sessions are safely prunable.
  They're a meaningful share of text content in some sessions, but they carry
  live instructions, so this needs care.
- Better metric for any of this: don't ask a downstream session whether it
  *noticed* an edit — ask it to **reconstruct** what happened in the edited
  region and diff against ground truth. Confabulation is the failure that costs
  you, and it's invisible to a noticing-based check: a session that cheerfully
  invents plausible history scores *better* on "didn't complain."
