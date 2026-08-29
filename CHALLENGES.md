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

## 9. The headline nearly shipped resting on a single seed

**What happened.** The whole submission was built, tested, documented and pushed with CI
green, and every rupee figure in it came from one batch: seed 42. Nothing was wrong with the
number. The problem was that nothing in the repository could tell the difference between
"routing by root cause beats a naive retry loop" and "routing by root cause beat a naive
retry loop on the batch I happened to generate first".

**Why it was worth stopping for.** The graded bar is *measured* money recovered. A single
observation is not a measurement, and a reviewer's first instinct on seeing +6.7% from one
seed is to wonder how many seeds were tried before that one. There was no way to answer
that from the repository.

**Solution.** `reclaim benchmark` runs the whole pipeline plus the whole baseline over N
independently generated batches in temporary directories, and reports the distribution with
the worst seed as prominent as the mean. 30 seeds takes 8 seconds. The result: 30 of 30
seeds recover more, median +11.0%, worst +0.7%, attempts down on every single seed, and hard
stops honoured on all 30.

**The bit worth keeping.** Seed 42 came in at +6.7%, which is *below* the median of the
distribution it belongs to. The README now says so explicitly. A headline that turns out to
be a below-average case is worth considerably more than one a reviewer has to take on trust,
and it would have been easy to quietly re-point the README at seed 17 (+79.7%) instead.

**Second-order.** Building the sweep exposed that `Classifier` writes its LLM cache to
`out/llm_cache.json` on flush, so a 30-seed sweep would have written into the directory the
report is built from. The sweep now runs entirely in a temporary directory with a disabled
cache, and a test asserts that running one leaves the working directory untouched.

## 10. Proving the win came from the design, not from somewhere else

**What happened.** The seed sweep established that the system beats the baseline on 30 of 30
batches. It could not say *why*. The README claimed root-cause routing was the reason, but
that was an argument, not a measurement, and several other things changed at the same time:
the retimed schedules, the customer nudges, the cost floor.

**Why it mattered.** If the advantage actually came from the retiming alone, then "route by
root cause" is the wrong headline and the classifier is expensive decoration. There was no
way to tell from the repository.

**Solution.** `reclaim ablate` disables one feature at a time and re-runs the full pipeline
plus baseline over identical seeds. Variants are produced by mutating a copy of
`config/policies.yaml` on disk and reloading it through the normal loader, so an ablation
exercises the same configuration path the real system uses rather than a special-cased
in-memory shortcut.

**What it found.** Routing is worth 17.9% of recovery, curve-aware timing another 15.9%,
nudges 8.2%. Removing routing and timing together drops the advantage over the naive
baseline from +23.6% to +2.0%. The headline survived contact with the measurement.

**The row I did not delete.** Disabling the cost floor changes recovery by +0.0% and
*increases* attempts by 1.9%. On first reading that looks like a feature not earning its
place. It is the opposite: the cost floor is a spend-control rule, and a spend-control rule
that moved the recovery number would be doing something wrong. The report says this in
prose next to the table, and a test asserts that a zero-cost variant is reported as
zero-cost, so the table cannot drift into flattering the design later.

**Trap avoided.** The first draft of the "no root-cause routing" variant replaced *every*
policy with one template, including `HARD_DECLINE` and `MANDATE_REVOKED`. That would have
had the variant retrying stolen cards, producing a far larger and completely dishonest
number: it would have been measuring recklessness, not routing. The variant now leaves hard
stops intact and a test asserts it.

## 11. Every documented number was one edit away from being wrong

**What happened.** Adding the seed sweep, then the cost accounting, then the ablation each
changed figures the README quotes. After the ablation landed I audited the README against the
artefacts by hand and found all 36 figures still matched, which was luck as much as care:
nothing in the repository would have caught it if one had not.

**Why it mattered more here than in most projects.** The claim being made is that these
numbers are measured, not asserted. A README that has silently drifted out of sync
undermines that claim more effectively than a missing feature would. A reader who spots one
stale figure has no way to tell which of the others are also stale, so they discount all of
them.

**Solution.** The hand audit became `reclaim verify-docs`. It collects every headline figure
from the run artefacts, the sweep and the ablation, formats each exactly as the README
presents it, and checks the document contains it. CI regenerates the seed-42 run and both
studies, then runs the check as a gate: a change that moves a number and does not update the
README now fails the build.

**Design choice worth recording.** It checks presence of the rendered string rather than
parsing the Markdown. The property worth enforcing is that the number a reader sees came out
of the pipeline, not that the prose has a particular shape; a parser would have made ordinary
prose edits fight the tool. The negative case is tested by mutating one figure in a generated
document and asserting the check fails on exactly that figure.

## 12. Two defects that only an adversarial read of the finished work found

With the submission complete, CI green and every claim validated across seeds, I read the
diff back looking for things a reviewer would hit. Two came out, neither of which any
existing test covered, and both of which would have been embarrassing to be shown.

**The escalation queue was not durable.** The audit log is append-only and permanent, but the
human work list it produces was written to one shared path, `out/escalations.jsonl`. Running a
second batch overwrote it. `verify-audit --run <older_id>` then failed with "queue has 0
records, log has 34 ESCALATED events", and `queue --run <older_id>` showed an empty queue for
a run that had escalated 34 cases. Reproduced by running two batches and verifying the first.
Fixed by writing the queue twice: the shared file stays as the working list the brief asks
for, and `out/escalations_<run_id>.jsonl` is that run's permanent copy, which the verifier,
the CLI and the report all prefer. Three regression tests; two of them fail without the fix,
which I checked by reverting it.

**The report manufactured a comparison it did not have.** With the baseline log missing, the
report fell back to `build_comparison(events, baseline_events or events)` and compared the run
against itself. The result was not an error or a blank: it was a confident "+₹0.00" delta
card, a baseline column of zeroes in the per-category table, and no indication anywhere that
the baseline was absent. A reader cannot distinguish that from a genuine measurement of no
improvement. On a page whose whole purpose is measured claims, inventing a number from a
missing file is the worst possible failure mode. The report now says "not measured", states
that it is making no claim, and drops the comparison columns; a test asserts the string
"+₹0.00" never appears in a baseline-less report.

**What I take from these.** Both are the same shape of mistake: a fallback that produces
plausible output instead of an honest absence. `or events` and a single shared filename are
each one character's worth of convenience that silently trades correctness for never having
to handle a missing case. Neither showed up in 168 tests, a 30-seed sweep, an ablation, or
green CI, because every one of those exercised the happy path where both files exist.

## 13. The verification tool needed verifying

**What happened.** Having built `verify-docs` and advertised it in the README and the pull
request as a gate that "fails the build on a single stale figure", I went back to ask whether
that was true. It was not.

The check searched the whole document for a bare substring. Three of the per-category figures
rendered as `"0"`, which matches any document containing a zero; six more were two or three
characters. So of 37 "verified" figures, at least five carried checks that could not fail
under any circumstances. The tool was reporting coverage it did not have, and I had repeated
that number publicly.

**Why this one stung.** Every other defect in this file was a bug in the product. This was a
false claim about the strength of a verification, made by me, in the document the
verification was supposed to protect. It is the exact failure mode the tool exists to prevent.

**Solution, in three parts.**

*Anchor each figure.* Every figure now carries a context string that must appear in the same
segment. Anchoring on the *line* was the obvious fix and wrong: prose wraps, so a figure and
its anchor routinely land on different lines, and the first attempt failed on a sentence that
happened to break after "and 30 on". The correct unit is the Markdown row or paragraph, so
`segment()` splits table rows and code lines out individually and rejoins wrapped prose.

*Stop counting checks that cannot fail.* A hard-stop category's recovered value is `0` inside
a table row of legitimate zeroes. No substring check on it can ever fail, so it is excluded
rather than counted. That those categories recover nothing is covered by the hard-stop
invariant, which is asserted in three places and gated in CI across every seed and variant.

*Measure the gate by mutation.* A test rewrites each of the 34 checked figures in turn and
asserts the check fails. All 34 are caught. Mutating only one occurrence catches 30 of 34,
because four figures are quoted in several places, and `"30"` appears twelve times in the
README.

**What the README says now.** Not "fails on a single stale figure" but: mutation-tested at 34
of 34; a figure quoted in several places is caught only when every occurrence is wrong;
unverifiable figures are excluded rather than counted, with a pointer to what does cover them.
Weaker, and true.

## 14. The same mistake, four times, and the sweep that found the fourth

By the third defect a pattern was obvious enough to search for deliberately: **a fallback
that produces plausible output instead of an honest absence.** Each one passed CI, and none
was caught by 177 tests, a 30-seed sweep or an ablation, because all of those exercise the
path where the data is present.

| # | Mechanism | What it produced instead of "I don't know" |
|---|---|---|
| 1 | One shared escalation file | An empty queue for a run that escalated 34 cases |
| 2 | `build_comparison(events, baseline or events)` | A confident `+Rs 0.00` delta from a missing file |
| 3 | A test guard that skips | A green CI run that verified nothing |
| 4 | `inputs.get("action_cost_paise", 0)` | `cost of acting: Rs 0.00`, net equal to gross |

The fourth came from grepping the codebase for the shape rather than waiting to trip over it:
`or` fallbacks, `.get(x, 0)` on values that reach a published number, and exception handlers
that swallow. It found `metrics.py` summing a missing cost field to zero. An audit log written
before cost tracking existed therefore reported a cost of zero and a net figure identical to
the gross one, with nothing distinguishing that from a genuinely cheap run. Reproduced by
stripping the cost stamps out of a real log and recomputing.

`RunMetrics` now carries `action_cost_recorded`, false when the log holds billable actions
with no cost stamp. The CLI prints "not recorded in this log, so no net figure is claimed" and
the report shows "not recorded" instead of a rupee value. Three tests, including one asserting
`0.00` never appears in that line.

The same sweep exposed a config-contract gap: `plan` defaults to an empty list when the key
is missing, so a typo in `policies.yaml` would produce a recoverable category that silently
does nothing and terminates as `plan_exhausted`. A test now asserts every recoverable policy
has a plan, a positive attempt cap and at least one channel, and that every non-recoverable
one has none of those.

**The lesson, stated for the form field.** Every one of these was a single expression written
to avoid handling a case that "cannot happen": a default argument, an `or`, a skip. In a
system whose entire claim is that its numbers are measured, the cost of that convenience is
not a crash, which would be honest, but a number that looks measured and is not. Searching
for the shape found more instances than waiting for failures did.

## 15. Two more from the same sweep, and the one that flattered the headline

**A typo in policies.yaml was silently accepted.** Writing `plann` instead of `plan` left
`INSUFFICIENT_FUNDS`, the largest recoverable category, with no steps at all. The run exited
0, printed a confident result, and recovered nothing from that category. Nothing in the
system distinguished it from a deliberate policy choice: the audit log faithfully recorded
`plan_exhausted` for every one of those cases, which is exactly what it should record for a
policy with no plan. An out-of-range value was caught by a Pydantic constraint; an unknown
key was not, because the loader read known keys and ignored the rest. The loader now rejects
unrecognised keys, suggests the intended one via `difflib`, and rejects missing required ones.
Four tests, one of which asserts the shipped configuration still loads.

**The recovery rate quoted in the README was a high outlier.** Checking whether the pipeline
held at scale, a 10,000-case batch recovered 40.2% of addressable value, against the 61.65%
the README quoted from seed 42. Recomputing the rate across the 30 sweep seeds: mean 44.6%,
median 44.8%, and 28 of 30 seeds below 61.65%.

This one mattered more than it first looked. I had already given the *delta* the full
distribution treatment and made a point of noting that seed 42 came in below the median, which
reads as commendable restraint. But I had not done the same for the *rate*, where the same
seed is at roughly the 93rd percentile. Reporting the spread only for the figure where the
headline happens to look modest, and a bare point estimate for the one where it looks good, is
selective honesty, and it is more corrosive than an outright error because it wears the
costume of rigour.

The sweep now reports the recovery-rate distribution alongside the delta distribution, the
report renders both, and the README says in as many words that seed 42 is a conservative
sample of the delta and a flattering sample of the rate, and that the median of 44.8% is the
number to use for how much of the addressable value this recovers.

## 16. The LLM path had never once been exercised against the real SDK

**What happened.** The classifier's Anthropic call was covered by a dozen tests, all of which
substituted a fake client. They proved the routing (rules first, model second, confidence
floor, cache) and proved nothing whatsoever about the request the SDK builds. `ANTHROPIC_API_KEY`
is unset in this environment and CI deliberately never sets it, so the genuine call had run
exactly zero times. A malformed request would have surfaced for the first time on a reviewer's
machine, on the first `--llm` run.

**What I did.** Drove the real `anthropic` client through an `httpx2` mock transport, so the
SDK serialises an actual request that a test can assert on, with no network and no key. (A
first attempt failed on `import httpx` — the 1.x SDK is built on `httpx2`, not `httpx`.)

**Four defects, found within minutes of the harness existing.**

*`max_tokens` was 300, and thinking is on by default on current models.* Thinking tokens count
against `max_tokens`. A thinking pass could exhaust the budget before the tool call was
emitted, leaving a response with no `tool_use` block, which my parser reads as "no usable
answer" and escalates. Every unmapped code would have escalated, billed, for no visible
reason. Fixed with low effort and a larger ceiling. Disabling thinking would have been the
wrong fix: on current models that can make the model write the tool call into visible prose,
which this parser would never see.

*Strict tool use was off.* The brief's requirement is "never let the LLM invent a category". I
enforced that in Python and stopped there; the API can enforce it too, and now does.

*A rejected API key degraded silently.* The handler that absorbs a model outage also absorbed
an authentication failure. Run `--llm` with a bad key and every unmapped code became `UNKNOWN`:
a full batch of escalations, exit 0, no indication the credentials were the problem. This is
the same defect shape as the shared queue file, the missing baseline, and the unrecorded cost —
a fallback producing plausible output instead of an honest failure — and it is the fifth
instance. A 500 still degrades, because an outage leaves the batch worth running; a bad key
does not.

*The model default was a cheaper one than I would choose deliberately.* Moved to the current
default; it stays in config so an operator can trade it down knowingly.

**The lesson.** A mocked collaborator tests your code's reaction to an answer. It never tests
the question you asked. Everything on the far side of that fake — the request shape, the
schema the API validates, what the SDK does with your parameters, which errors it raises — was
unexamined, and four things were wrong in it.

## 17. Test fixtures that were quietly writing to the project's output directory

**What happened.** The `Classifier` writes its LLM cache to `out/llm_cache.json` on flush.
Tests constructing a classifier were therefore touching the real `out/` directory, which
would have made test runs order-dependent and could have polluted a demo run's artefacts.

**Solution.** A disk-free cache stand-in in `tests/conftest.py`, used by every test-built
run. Separately, `build_run` moved out of `conftest.py` into `tests/helpers.py`, because
pytest's conftest is not an importable package and `from .conftest import ...` fails
collection.

## 18. A correct control that nothing was holding up

**What happened.** A security review of the finished diff found no exploitable defect:
`yaml.safe_load` at both call sites, no `eval`/`exec`/`pickle`/`subprocess` anywhere in
`src/reclaim`, the API key read from the environment and never interpolated into a log
line or an error message, paths built only from trusted CLI flags and an
internally-derived `run_id`.

What it did find was a gap. Two of the strings the HTML report renders are not written by
this codebase. `case_id` arrives in the batch file, and `--batch` accepts any path, so it
is attacker-controlled through the tool's own documented interface. The classifier's
`rationale` is free text from a language model, produced in response to a
`decline_description` that itself came from the batch, and `engine.py` stamps it straight
into the audit event's `detail`, which the report renders. Both were escaped correctly in
every single place. Neither was asserted anywhere. The property was held up by whoever
last edited a template string remembering to type `_esc()`.

**Solution.** `tests/test_report_escaping.py` poisons both, runs the real engine, renders
the real report and asserts no tag-forming sequence survives. Deleting either `_esc` call
fails it; I checked by deleting them.

**Two things the tests corrected in my own first draft**, both the same mistake:

Asserting `onerror=alert(2)` was absent is the wrong check. That substring survives
escaping and is completely inert, because `&lt;img ...&gt;` is text. What must never
survive is a sequence a parser reads as a tag, so the assertion is on `<script` and
`<img`.

The rationale test passed vacuously at first. Blanking the decline code was not enough to
reach the model: the generator's description still says "insufficient funds", a keyword
pattern matched it, the rule layer answered, and the model was never consulted. Every
test here asserts both that the payload is neutered *and* that its escaped form is on the
page. The second assertion is the only thing between this and a guard over nothing, and
it is what caught it.

**A stale number found in the same pass.** The README claimed 214 tests and the PR body
claimed 186; the suite has 217. `reclaim verify-docs` regenerates a run and diffs every
figure the README quotes against the audit log, but a test count is a repository fact,
not a run fact, so the gate could not see it. `scripts/check_readme_counts.sh` now derives
it from pytest collection, in `make ci` and in CI, and fails on a missing claim as well as
a wrong one — deleting the sentence does not make the check pass.

Coverage is floored at the claimed figure via `fail_under`. It stays 94%, not 95%: actual
coverage is 94.58% and the terminal report rounds that to "95%". I had already edited the
README to say 95 off that rounded display before checking the real number. A floor claim
truncates.

**Why this belongs in this file.** Nothing here was broken. Every one of these is the
same shape as the six defects in §14 — a number or a property that looked established and
was resting on convention. The lesson repeated is that "it is correct everywhere I
looked" and "the build will tell me when it stops being correct" are different claims,
and only the second one survives someone else editing the file.
