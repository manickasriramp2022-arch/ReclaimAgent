# Challenges

Technical obstacles actually hit while building ReclaimAgent, in the order they happened,
and how each was solved. Written as the build went, not reconstructed afterwards. Several of
these are cases where the first implementation was wrong in a way that the metrics made
visible only once the whole pipeline ran end to end.

---

## 1. The first end-to-end run showed the agent losing to the naive baseline

**What happened.** The first complete run recovered ₹577,565 against the naive
retry-everything-three-times baseline's ₹667,031. The agent was worse, by ₹89,466.

**Why it mattered.** This is the number the whole submission rests on. The temptation is to
tune the simulator until the agent wins. That would have made every figure in the report
meaningless, so instead the gap was treated as a bug report and traced to its causes. It
turned out to be three separate defects plus one honest finding.

**Defect 1: contact refusals were killing the charge path.** When the compliance layer
terminally refused a customer-contact step — no consent on file — the engine stopped the
entire case. But consent gates *messaging a customer*; re-presenting an existing
authorisation is a different permission entirely. 20 cases in 250 were being written off
because the agent could not send them an email. Fixed by making a terminal refusal end the
case only when it lands on the charge rail; a refused nudge now just drops that step and the
plan continues. The one exception is a policy where contact *is* the recovery path
(`EXPIRED_CARD`), where losing contact legitimately ends the case.

**Defect 2: the attempt cap was double-counting.** `max_attempts_per_case` counted attempts
made before the batch as well as attempts made during the run, so a case arriving with three
prior attempts got zero. But counting prior attempts is the *network ceiling's* job, and the
compliance layer was already doing it independently. The policy cap now counts only this
run's attempts, and `card_network.max_retries_per_declined_authorisation` remains the total
ceiling. Two ceilings, independently auditable, neither doing the other's work.

**Defect 3: the insufficient-funds policy retried at the worst possible moment.** The plan
re-presented at +6 hours. The simulator's balance-recovery curve is at 0.45–0.80 of its base
rate there and peaks around +168 hours. The agent was spending its first and best attempt at
the bottom of the curve while the baseline, retrying at a flat 24/48/72 hours, accidentally
landed higher. Retimed to nudge at +4h and re-present at +40h, +122h and +170h. Same number
of attempts, placed where the money is. This is now the single largest source of the
like-for-like delta.

**The honest finding.** After all three fixes the agent still recovered less *gross* than the
baseline, and always will, because the baseline debits revoked mandates, skips pre-debit
notification windows and ignores AFA thresholds. It takes ₹71,084 that way across 85 debits
the compliance layer had already refused. That is not a defect to fix, it is the cost of
compliance, and burying it would be dishonest. The report now states two comparisons: a
like-for-like delta over the cases both strategies were permitted to work (**+₹32,042 on 169
fewer attempts**), and the gross gap with its cause named. See `DECISIONS.md` §5.

## 2. The circuit breaker halted a perfectly healthy batch

**What happened.** After retiming the policies, a run halted at attempt 174 of 385, stopped
169 open cases and escalated the lot. Recovery collapsed from ₹513,660 to ₹310,538.

**Diagnosis.** The breaker fired on "failure rate ≥ 97% over the last 60 attempts". Reading
back the window from the audit log showed what it actually contained: 21 third attempts on
soft declines, 12 second and 12 third attempts on insufficient funds, 12 first attempts on
expired cards. Those are the *tail* of a decay curve. A soft decline's third attempt has a
true success probability around 0.039 by construction. Sixty of those in a row producing one
success is not an outage, it is the model working as designed.

**Solution.** A failure-rate threshold alone is not a circuit breaker, it is a decay-curve
detector. The breaker now compares observed successes against the successes the engine's own
priors predicted for exactly those attempts, and fires only when all of the following hold:
at least 40 attempts made, failure rate ≥ 99% over the window, at least 5 successes
*predicted* in the window, and observed successes below 25% of that prediction. A hard cohort
fails a lot but roughly as forecast, so it passes; a dead acquirer produces zero successes
against a real forecast, so it trips. Both directions are tested, including a test that
zeroes every success rate in `config/simulation.yaml` to force a genuine outage.

**Second-order finding.** Fixing the breaker exposed a related problem: the engine's priors
in `config/policies.yaml` were two to four times the simulator's actual probabilities, which
is what made the expectation shortfall look catastrophic. Those priors also drive the
`cost_floor` stopping rule, so they were miscalibrated there too. Recalibrated to within
roughly 1.3× of the simulator's real values — deliberately close but not identical, because
the engine is supposed to act on an imperfect belief, not on the answer key.

## 3. The batch's failure window was washing out the timing signal

**What happened.** The `TECHNICAL_ERROR` policy specifies a fast backoff — +1h, +4h, +24h —
because a transient gateway fault clears quickly. But the simulator measures elapsed time
from the *original* failure, and the generator spread failure timestamps across a 72-hour
window ending at the batch anchor. So a "+1 hour" retry was landing anywhere from 1 to 73
hours after the failure it was retrying. The entire short-backoff story was noise.

**Solution.** `generator.attempt_window_hours` reduced from 72 to 18. A recovery agent works
recent failures; an overnight batch does not contain three-day-old ones. The config comment
explains the reasoning so the value does not get "tidied" back up later.

## 4. A network constant was silently overriding a policy the config claimed to enforce

**What happened.** Replaying a technical-error case revealed the `rolling_window_attempt_cap`
(one charge per 24 hours) and `card_network.min_hours_between_retries` (24 hours) both
firing on it. The policy declared a backoff of 1, 4 and 24 hours. What the system actually
did was 1, 25 and 49 hours. The config was describing behaviour the system was not
performing.

**Solution.** Card-network retry rules are written for *declined authorisations*. A gateway
timeout returned no issuer decision at all, so a spacing rule for declines does not apply to
it. Added `card_network.retry_spacing_exempt_categories` to the compliance config and
`exempt_categories` to the rolling-window rule, both listing `TECHNICAL_ERROR`, both with the
reasoning written into the config file. The exemption is itself marked `unverified: true`,
because the reasoning is sound but the mapping to a real acquirer's response codes is not
something this project can confirm.

**What this cost.** Finding it required reading a `reclaim replay` transcript line by line.
It would not have shown up in any aggregate metric — the case still recovered, just later
than the config promised. That is an argument for `replay` existing at all.

## 5. Two audit logs, two different orderings, one verifier

**What happened.** `verify-audit` checks that timestamps are non-decreasing in sequence
order. The main engine satisfies this naturally because it drains a time-ordered event queue.
The baseline runner, written case-by-case, emitted case 1's attempts at T+24/48/72 and then
started case 2 back at T+0. Timestamps went backwards on every case boundary and the
verifier failed the baseline log.

**Two options.** Relax the check to cover only the treatment log, or restructure the baseline
to be time-major. The first was tempting and wrong: a structural invariant that only holds
for the code path you wrote carefully is not an invariant. The baseline was restructured to
ingest and classify everything first, then run each retry round across the whole batch. One
verifier, both logs, no exceptions.

**Related.** The two safety checks — zero attempts on hard-stop categories, zero on `UNKNOWN`
— are *expected to fail* on the baseline log, because the baseline is deliberately
non-compliant. `verify-audit` handles this by asserting that the baseline's structural checks
all pass and that its safety checks all fail. A baseline that accidentally honoured hard
stops would make the comparison vacuous, so that is now a test failure too.

## 6. Determinism versus a wall clock in the audit trail

**What happened.** The brief requires "same seed → identical audit log" with a test asserting
it, and separately requires an ISO-8601 UTC timestamp on every event. A real audit trail
wants wall-clock time. Wall-clock time makes byte-identical logs impossible.

**Solution.** The audit log runs entirely on the simulated clock, and wall-clock metadata for
the run lives in `out/manifest_<run_id>.json` outside the log. The determinism test is then a
`read_bytes()` comparison of two audit files, which is a much stronger claim than comparing a
normalised projection would have been. The cost is written down in `DECISIONS.md` §1: a
production deployment has to add per-event wall time back and weaken that test.

The same reasoning drove making the run id deterministic —
`<seed>-<sha256(case ids + config fingerprint)>` — which had a useful side effect: editing
any config file changes the run id, so a policy change cannot silently overwrite the audit
log of a run made under the old policy.

## 7. Making a report that cannot drift from its own log

**The problem.** It is easy to write a report from the engine's in-memory state and easy for
that to diverge from the audit trail. The brief requires metrics computed "from the audit
log, never from in-memory state", and requires `verify-audit` to prove it.

**Solution.** `metrics.compute_metrics` takes a list of `AuditEvent` and nothing else. It has
no access to the engine. `reclaim run` computes metrics from the events it just wrote and
saves them to `out/metrics_<run_id>.json`; `reclaim verify-audit` throws that file away,
recomputes every field from the JSONL on disk, and diffs field by field. The report reads
only from disk, so `reclaim report` works on a run from days ago.

Getting this to hold required one schema change: terminal compliance refusals had to be
distinguishable in the log from ordinary policy stops, or the addressable-value denominator
could not be reconstructed. Rather than adding a top-level field used by one case, the stop
event carries `inputs.terminal_class`. A test tampers with the reported metrics file and
asserts `verify-audit` catches it.

## 8. `mypy --strict` against a heavily overloaded SDK

**What happened.** The Anthropic SDK's `messages.create` is overloaded across streaming and
non-streaming shapes with a large union of tool parameter types. Passing a plain
`dict[str, Any]` tool definition failed strict type checking with a wall of overload
candidates, and the obvious `# type: ignore[arg-type]` was then reported as unused because
the real error was a `call-overload`.

**Solution.** The request is assembled as a single typed mapping and handed to the SDK in one
place, which confines the impedance mismatch to one line instead of spreading suppressions
across five keyword arguments. The tool schema itself stays a module-level constant that a
test checks against `RootCause`, so the closed set cannot drift from the enum.

Two smaller strictness fixes in the same pass: a named lambda in `metrics.py` (flagged as an
untyped call in a typed context) became a local function with a `Callable[[str], bool]`
parameter, and `_Case.policy` got a real `CategoryPolicy | None` annotation instead of a
`var-annotated` suppression.

## 9. Test fixtures that were quietly writing to the project's output directory

**What happened.** The `Classifier` writes its LLM cache to `out/llm_cache.json` on flush.
Tests constructing a classifier were therefore touching the real `out/` directory, which
would have made test runs order-dependent and could have polluted a demo run's artefacts.

**Solution.** A disk-free cache stand-in in `tests/conftest.py`, used by every test-built
run. Separately, `build_run` moved out of `conftest.py` into `tests/helpers.py`, because
pytest's conftest is not an importable package and `from .conftest import ...` fails
collection.
