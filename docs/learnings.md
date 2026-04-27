# Learnings Log

Engineering learnings from building this agent. Sanitized for forks — operator-specific incident notes belong in `CLAUDE.local.md`, not here.

Each entry records what was tried, what broke, why, and what we changed. Newest first. The intent is not a changelog (use `git log` for that) but a record of the *non-obvious* things — the bugs that taught us something about how Anthropic's API actually behaves, how operators actually use the CLI, or how the architecture's assumptions held up against reality.

---

## 2026-04-27 — Paste-fed runaway and the deterministic approval gate

**The runaway.** Operator pasted a multi-line block of prior-session output into the running CLI. Python's `input()` reads one line at a time, so every newline in the paste arrived as a separate "operator turn." The Conductor's tight `speak → input → speak → input` loop became a self-feeding pipeline: it consumed paste fragments like `"Encryption** |"` and `"WPA2-Personal"` as discrete operator messages, generated helpful agent prompts in response (*"Got it — more rows?"*), which consumed more paste, etc. Eventually the model emitted *"That reads as your approval. Locking it in now."* in response to garbled fragments.

**Why nothing was applied.** The conductor's tool registry is read-only + local-memory-write; no UniFi-write tool was wired. The "✅ Approval logged" was pure speak text, theater. But: if a write *had* been wired, this scenario would have synthesized authorization through model interpretation of ambiguous input. That's the prompt-injection threat made physical.

**Two defenses landed.**

1. **Bracketed paste detection in the CLI.** Two layers:
   - PRIMARY: enable bracketed-paste mode (`\e[?2004h`) at startup. Modern terminals wrap pastes in `\e[200~ ... \e[201~`; the renderer accumulates everything between markers as one operator turn.
   - FALLBACK: burst detection via `select`. After reading a line, poll stdin for ~50ms; if more lines are queued, they arrived as part of the same paste (interactive typing has 100ms+ gaps). Concatenate and return as one input.
   - On non-TTY stdin (tests, pipes), skip both layers and read normally.

2. **Deterministic approval gate.** New module `tools/approval_gate.py`. Tools are marked `requires_approval=True` in their `ToolSpec`. When the LLM emits a `CallToolDecision` for one, the agent loop:
   1. Generates a fresh random N-digit code (`secrets.randbelow`).
   2. Renders the *actual args the model is about to call with* + the code, directly to the operator.
   3. Reads paste-safe operator input.
   4. Compares byte-strict (after stripping whitespace) — no substring match, no inference.
   5. Match → consumes the gate atomically and runs the tool. Mismatch → tool refuses, returns `approval_denied` tool_observation.
   - Gate state is held in deterministic Python; the LLM never sees code generation or matching. Speak text claiming approval is ignored.
   - One-strike cancellation: a wrong code voids the pending approval (no slow-guessing under the TTL).
   - One approval, one write: `consume()` clears the gate; a second write needs a fresh challenge.

**Threat model the gate defends.** Prompt injection of approval phrases ("yes", "approve") in operator messages, tool outputs, durable memory, paste buffers. Hallucinated approvals from the model. Anything where the LLM's interpretation of "did the operator approve?" is the gate.

**Threat model the gate doesn't defend.** Operator with the typed code visible to a third party — by design, the operator is the trust anchor. Race conditions in shared terminals (single-operator interactive flow assumed).

**Lesson.** Whenever a write path is going to live, the question to ask is: "Where in this call chain does the LLM make the trust decision?" If the answer is anywhere, the design is broken. Deterministic Python must be the gate; the LLM can announce intent and shape the conversation, but cannot be the judge of its own authorization.

---

## 2026-04-27 — Live UX bundle: graceful API limits, cache visibility, save_fact discipline

**Context.** After the working_memory truncation fix, the next live session ran cleanly through ~16 model turns including a multi-step proposal flow. It died on the *very last* API call when the operator typed YES — Anthropic returned a workspace-limit 400.

**Three things we changed.**

1. **Friendly error handling.** Previously a 400 surfaced as `DoneDecision(reason="LLM API error: BadRequestError")` with a stack trace in logs and nothing actionable for the operator. Added `_classify_api_error` in `conductor_llm.py` that detects workspace-limit, rate-limit, billing, auth, and overloaded errors. The classifier returns a friendly message; the conductor queues a SpeakDecision with the message and a DoneDecision after it, so the loop renders the explanation before exiting. Reset dates from the body are surfaced verbatim — no log-spelunking required.

2. **Prompt-cache visibility.** We were marking system blocks as `cache_control: ephemeral` but never logging whether the cache was actually engaging. Added `cache_creation_input_tokens` / `cache_read_input_tokens` to the `api_response_received` debug event. Without this, you can't tell whether your token spend on long sessions is paying for cache hits or silently re-billing the full prompt every turn.

3. **save_fact discipline for personal identity.** Live trace showed the model inferred `operator_name=Taylor` at confidence 0.85 because the operator named a guest SSID "Taylor Guest Portal." That's a leap — the name could be anyone in the household. Tightened the prompt: person-identity inferences require operator confirmation before save_fact, OR cap at 0.5 confidence with the inference path in evidence verbatim. Network facts can stay aggressive; person facts must not.

**The bigger principle.** Confidence ratings on save_fact are easy to inflate when the model is being helpful. The fix isn't telling the model to "be careful" — it's giving it specific, falsifiable rules ("if it's a name, ask first") it can apply mechanically.

---

## 2026-04-27 — Working memory truncation silently dropped tool results

**Symptom.** Mid-audit, the model would receive `(tool result missing)` placeholders for `cite_corpus` and `record_caution_marker` — both tools that had successfully run. Then the model would emit empty responses (output_tokens=2, no content blocks) and the session would limp toward a confused end.

**Root cause.** The conductor's fold tracked which working-memory turns it had processed via an integer index (`_processed_turn_count`). But `WorkingMemory.recent()` truncates to `max_turns=12`. Once a session crossed the 12-turn cap, the stored index pointed *past* the truncated list, `working_memory[_processed_turn_count:]` returned `[]`, no tool_observations were folded, and every subsequent real tool_use got the defensive missing-placeholder. The model saw the placeholders, lost the thread, and went silent.

**Fix.** Track folded turns by `turn_id` (uuid) in a set, not by index. The Turn class already carried a uuid; we just weren't using it. Robust to truncation by construction.

**Lesson.** Two indexing schemes living in the same data flow (a turn count outside, a truncated list inside) is a class of bug. If a buffer has overflow semantics, every consumer that holds a position into it needs to use stable identifiers, not indices.

---

## 2026-04-27 — Virtual tools need synthesized tool_results too

**Symptom.** Anthropic returned 400: `"tool_use ids were found without tool_result blocks immediately after: toolu_..."` and the offending id was a `speak` tool_use.

**Root cause.** We were treating `speak` / `ask_operator` / `save_fact` / `log_decision` / `done_for_now` as *virtual* tools — advertised in the tool list, parsed into AgentDecision shapes, but never executed against a real implementation. The original design assumed Anthropic wouldn't require tool_result blocks for tools the model emitted but the loop didn't execute. Wrong: Anthropic enforces *every* tool_use have a matching tool_result on the very next user message, period. No exceptions for virtuals.

**Fix.** Track all tool_use IDs (real and virtual) in `_pending_tool_uses` with a `kind` tag. On fold, virtual tools get a synthesized `"ok"` tool_result; `ask_operator` is special-cased to put the operator's actual reply in the tool_result content (cleaner than synth + a separate text block). Real tools get the actual tool_observation content.

**Lesson.** When the API surface advertises `tools`, it really means *tools* — the runtime correlation rules apply uniformly. If you're modeling control-flow primitives as fake tools to get the model's tool-use machinery, you owe the API a tool_result for each one.

---

## 2026-04-27 — Multi-decision response queuing + interjection ordering

**Symptom.** When the model returned `[text + tool_use_A + tool_use_B]` in one response, the conductor was returning a single SpeakDecision and silently dropping the tool calls. Or worse: returning a CallToolDecision and dropping the speak so the operator never heard the narration.

**Fix.** Parse the response into an ordered list of decisions and queue all but the first; the loop drains the queue across subsequent `decide()` calls without making new API calls. First call after a multi-block response returns the speak; the loop renders it; next call returns the tool. Operator sees narration *before* the tool runs, which is what they want.

**Related ordering bug.** When the operator interjects during the speak, working_memory gets `[assistant, user_interjection, tool_observation]`. If the fold appends in working-memory order, the user_interjection lands *between* the assistant's tool_use and the tool_result — Anthropic 400's because tool_result blocks must immediately follow their tool_use.

**Fix.** Two-pass fold: first emit tool_result blocks for all pending tool_uses (in document order), then any remaining user-text turns. Order in api_messages becomes `[assistant: [text, tool_use], user: tool_result, user: interjection]`, which satisfies the alternation invariant.

**Lesson.** The model speaks in *document order within one response*. The loop processes decisions in *response order across many turns*. Bridging those two timelines requires being explicit about which user inputs pair with which assistant outputs — pairing by reverse document order for asks, then text-block fallback for un-paired interjections.

---

## 2026-04-27 — JSONL debug logging is non-negotiable

**Lesson.** After three live failures with mystery 400s, we built `tools/conductor_debug.py` — a JSONL trace of every API boundary, full request payload + Anthropic's error body, response content blocks, decision queue state. Path: `logs/conductor_debug.jsonl` (gitignored).

Without it, debugging an Anthropic 400 means re-running the session and hoping the bug repeats with the same shape. With it, every 400 is `tail -1 logs/conductor_debug.jsonl | jq` and the answer is sitting there: which message had which malformed block, what tool_use_ids were unmatched, what the model actually emitted.

The cost of writing it was an hour. The cost of *not* writing it earlier was several failed sessions where the only artifact was a stack trace.

**Principle.** For any non-trivial API client where the error path is opaque (tool correlation, schema validation, alternation), full request+response logging at the boundary is table stakes. Don't wait until you've failed three times in a row.

---

## 2026-04-27 — One Conductor, many tools (architecture pivot)

**Context.** Earlier iteration had separate agents (Auditor, Optimizer, Security, Onboarding, etc.) each with their own LLM-driven loop. Operator quote: *"this WAs all meant to be AGETNIC WTF do you build?"*

**Decision.** Single Conductor agent that talks to the operator. Auditor / Optimizer / Monitor become deterministic *tools* the Conductor calls. The operator only ever talks to one LLM-driven conversational agent; specialized work happens behind tool calls.

**Why.** Multiple LLM-driven agents communicating through a queue means N×M failure modes — one agent's confusion infects another's working memory; tool correlation across agent boundaries is twice as fragile; the operator gets handed off mid-conversation and loses context. A single Conductor with deterministic specialist tools means the LLM machinery (correlation, alternation, virtual-tool-results) only has to be solved once.

**Trade-off.** The Conductor's prompt + tool list are bigger than any individual specialist agent's would have been. Token cost per turn went up. But token cost is easy to optimize (prompt caching); correctness across agent boundaries is not.

---

## How to add to this log

When you discover something non-obvious — a bug whose root cause taught you something about the API or the architecture, a UX failure mode that changed your prompt design, an architectural decision made under fire — add an entry. Lead with the symptom (so a future reader scanning can find it), then root cause, then fix, then the *lesson* in plain English.

Don't add entries for things that are already obvious from `git log` or the code. The log is for the part you can't recover by reading the source.
