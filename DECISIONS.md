# Decisions

Every choice the brief left open, what was chosen, and why. Where a decision has a cost,
the cost is stated.

---

## 1. Time is simulated, and the audit log carries no wall clock

**Decision.** A run executes against a virtual clock. `T0` is one hour after the latest
failure in the batch; the engine drains a time-ordered event queue forward over a bounded
horizon (21 days by default). Audit timestamps are simulated-clock instants. Wall-clock
metadata lives in `out/manifest_<run_id>.json`, outside the log.

**Why.** Two reasons, and the second is the important one.

A policy with a 170-hour backoff cannot be demonstrated in real time. More importantly, the
brief requires "same seed → identical audit log" and a test asserting it. If the log carried
`datetime.now()`, that test could only compare a normalised projection of the log, which is a
much weaker claim. With no wall clock and no random identifiers in the log, two runs produce
**byte-identical files**, and the test is a `read_bytes()` comparison.

**Cost.** A production deployment needs real timestamps in the audit trail. The manifest
holds the wall-clock bracket for the run, but per-event wall time would have to be added
back, and the determinism test would then compare a projection.

## 2. The run id folds in a config fingerprint

`run_id = <batch_seed>-<sha256(case ids + config fingerprint)[:8]>`. Editing any config file
changes the run id, so a policy change cannot silently overwrite the audit log of a run made
under the old policy. A test asserts this.

## 3. `actor` has six values, not the four the brief listed

The brief names `rule`, `model`, `policy` and `human_queue`. Two were added: `compliance`
and `simulator`.

**Why.** Attributing a refusal to the compliance layer rather than to the policy engine is
what makes refusal counts auditable — "who refused this and under which constant?" is a
different question from "which policy stopped this case?". And attributing an outcome to
`simulator` keeps simulated results visibly separate from decisions, everywhere in the log.
Grep the log for `"actor":"simulator"` and you have found every line that is not a real
decision.

## 4. Recovery rate is measured against addressable value, and the gross rate is printed too

**Decision.** `addressable = value_at_risk − value of cases the compliance layer terminally
refused on the charge rail`. The headline rate is measured against addressable. The gross
rate appears next to it in every output.

**Why.** The brief requires that refusals "must not pollute the recovery-rate denominator".
But choosing a denominator is exactly the move a dishonest submission makes, so both are
always reported, and `verify-audit` recomputes both from the log.

**Subtlety.** Only *charge-rail* refusals are subtracted. A contact refusal removes one
nudge, not the case, so those cases stay in the denominator. See decision 6.

## 5. The baseline comparison is reported two ways, and the unflattering one is not hidden

**Decision.** The headline delta is like-for-like: only the cases the compliance layer
permitted ReclaimAgent to work at all. The full-batch figures are reported alongside, and on
the batch the naive baseline "recovers" more.

**Why.** The baseline debits revoked mandates, skips pre-debit notification windows and
ignores AFA thresholds. It gets ₹71,084 that way. Netting that against ReclaimAgent's
recovery, or quietly excluding it, would make the delta a lie in either direction. So the
report states the gross gap, names its cause, and puts the like-for-like row forward as the
fair comparison. A recovery agent that beats its baseline by ignoring the rules has not
solved the problem, and a report that hides the gap has not either.

## 6. Consent gates contact, not the charge rail

**Decision.** A terminal compliance refusal ends the case only when it lands on
`retry_charge`. A refusal on a contact channel removes that nudge and the plan continues to
its remaining charge steps. The exception is a policy that lists `contact_cap` in its
terminal conditions *and* has no charge step left — for `EXPIRED_CARD`, customer contact is
the whole recovery path, so losing it ends the case.

**Why.** Re-presenting an existing authorisation and messaging a customer are different
permissions. The first build conflated them and stranded 20 recoverable cases in a 250-case
batch. See `CHALLENGES.md`.

## 7. The engine's success priors are separate from the simulator's ground truth

**Decision.** `config/policies.yaml` holds `cost_floor.expected_success_prior`, the engine's
belief about whether an attempt is worth making. `config/simulation.yaml` holds what
actually happens. The engine never reads the simulation config. The two tables are close but
deliberately not identical.

**Why.** If the cost floor consulted the simulator it would be grading its own homework. A
production system calibrates priors from recovery history and is wrong by some margin; that
margin is what the stopping rule has to survive.

**Cost.** Miscalibration between the two is a real failure mode and it bit during the build:
priors roughly three times the simulator's truth made the circuit breaker misfire. See
`CHALLENGES.md`.

## 8. The circuit breaker requires a collapse against forecast, not just a bad streak

**Decision.** `batch_circuit_breaker` fires only when three conditions hold together: at
least 40 attempts have been made, the failure rate over the last 60 attempts is at or above
99%, the engine's own priors predicted at least 5 successes in that window, and observed
successes are below 25% of that prediction.

**Why.** A failure-rate threshold alone is not a breaker, it is a decay-curve detector. The
tail of a soft-decline cohort fails at 96–99% by construction, and a naive threshold halted
a healthy 250-case batch and stranded 169 recoverable cases. "Doing far worse than we
predicted" distinguishes a dead acquirer from a hard cohort. Both the fire and no-fire cases
are tested.

## 9. Insufficient-funds retries are scheduled late, not fast

**Decision.** The `INSUFFICIENT_FUNDS` plan nudges at +4h and re-presents at +40h, +122h and
+170h, rather than retrying immediately.

**Why.** The failure is a timing problem. Retrying at +6h spends an attempt at the bottom of
the balance-recovery curve. This is the single largest source of the like-for-like delta
against a baseline that retries at a fixed 24/48/72 hours: same number of attempts, placed
where the money is.

## 10. Batch failure timestamps span 18 hours, not 72

**Decision.** `generator.attempt_window_hours: 18`.

**Why.** A recovery agent works recent failures. A 72-hour spread smears every case's "hours
since original failure" across a 72-hour band, which washes out the timing signal the
policies exist to exploit and makes the short backoff for technical errors meaningless. 18
hours is a realistic overnight-batch window.

## 11. Contacts are modelled as an uplift on later charges, not as a recovery channel

**Decision.** A dunning email, SMS or update-payment-method link never recovers money
directly. It has a configured probability of the customer acting, and acting multiplies the
success probability of every subsequent charge attempt on that case (1.9× for
`INSUFFICIENT_FUNDS`, 7× for `EXPIRED_CARD`).

**Why.** It is how these channels actually work, and it keeps every rupee of recovered value
attributable to exactly one `CHARGE_ATTEMPT` event, which is what makes the metrics
reconstructible from the log without double counting.

## 12. Escalation priority is value weighted by recoverability, not value alone

`priority_score = amount_at_risk × recoverability_weight[category]`, with weights in
`config/policies.yaml`. A ₹50,000 hard decline (weight 0.05) should not outrank a ₹8,000
expired card (weight 0.75) in a human's queue: one of them a human can fix.

## 13. Hard-stop cases are not escalated by default, unless they are large

`escalate_terminal_above_paise: 2000000` (₹20,000). A stolen card is not a human's problem,
so escalating all of them would drown the queue in noise. A ₹20,000 stolen card probably is
worth a look. The threshold is config, not code.

## 14. `UNKNOWN` is terminal, not "retry cautiously"

An unclassified decline goes straight to a human with the raw code attached. The cost of
retrying something the system does not understand is unbounded; the cost of a human looking
at ten cases is not. The recommended action names the file to edit
(`config/decline_rules.yaml`) so the fix is a config change.

## 15. The decline codes are synthetic and named to look like a taxonomy, not copied

Codes like `NF_INSUFFICIENT_FUNDS` and `SD_DO_NOT_HONOUR` are modelled on the *shape* of
common gateway decline taxonomies. They are not copied from any live gateway's code list and
`config/generator.yaml` says so. The rule layer maps codes to categories through a config
table, so re-pointing it at a real gateway's codes is a config change.

## 16. Two decline codes exist specifically to be unmappable

`XX_UNSPECIFIED_1904` and `XX_BANK_MESSAGE`, ~4% of the batch. They exercise the LLM
fallback when a key is present and the `UNKNOWN` → escalation path when it is not. A
generator that never produced an unmappable code would leave the safest path in the system
untested.

## 17. The LLM fallback is constrained by a tool schema, not by prompt instructions

The category is an `enum` in the tool's input schema, `tool_choice` forces the tool, the
answer is re-validated against the closed set in Python, and anything below the confidence
floor becomes `UNKNOWN`. A model outage returns `UNKNOWN` rather than raising. Three layers
of the same guarantee, because "the model must not invent a category" is a correctness
property, not a prompt-engineering preference.

## 18. LLM answers are cached by decline code, on disk

Cache key is the decline code alone, not the full transaction, because the classification
depends only on the code and its description. A 250-case batch makes at most one call per
distinct unmapped code — two, for this generator. The cache persists to
`out/llm_cache.json`, so re-running a batch costs nothing.

## 19. The default LLM mode is "auto"

`reclaim run` uses the fallback when `ANTHROPIC_API_KEY` is set and rule-only when it is
not. `--no-llm` forces offline; `--llm` forces on and fails loudly without a key. CI and
`make demo` both use `--no-llm`, so the demo cannot fail live.

## 20. `argparse`, not `click` or `typer`

The CLI has six subcommands and no interactive behaviour. Runtime dependencies are Pydantic
and PyYAML only; the Anthropic SDK is an optional extra. A reviewer cloning this repository
should not have to trust a dependency tree to run the demo.

## 21. The audit file is opened `"w"`, and that is still append-only

A run owns its own audit file and creates it once. Within the run's lifetime the handle is
append-only: no code path seeks, truncates or rewrites a line, and the `AuditLog` class
exposes nothing but `append`. Immutability across runs is guaranteed by the run id changing
whenever the batch or the config does, and immutability within a file is enforced by the
hash chain, which `verify-audit` checks. Opening `"a"` would have made a re-run silently
concatenate two runs into one file, which is worse.

## 22. `plan` is the authoritative schedule; `backoff_hours` is documentation

Each policy carries both. The engine executes `plan`, whose `after_hours` values are
measured from case start so the list reads as the backoff schedule. `backoff_hours` restates
the charge-step timings for readability, since the brief asks for a backoff schedule as a
named policy field. A test asserts plan steps are in ascending time order and that no plan
schedules more charges than its own attempt cap.

## 23. The headline is validated across seeds, not asserted from one

**Decision.** `reclaim benchmark --seeds 30` re-runs the whole comparison over 30
independently generated batches and reports the distribution: win rate, mean, median, and
the worst seed given the same prominence as the mean. `make demo` runs it, the HTML report
renders it, and CI sweeps 12 seeds on every push.

**Why.** Every rupee figure in this project rests on a simulated model. The one thing that
would make the headline worthless is if the delta were an artefact of the particular batch
that happened to be seed 42. The sweep is what converts "we recovered ₹32,042 more" from an
anecdote into a claim with an error bar attached.

**What it found.** 30 of 30 seeds recover more, median +11.0%, worst +0.7%, and seed 42's
+6.7% sits below the median. That last fact is now stated in the README, because a headline
that turns out to be a below-average case is worth more to a reviewer than one they have to
take on trust.

**Cost.** The worst seed is +0.7%, which is nearly nothing. The README says so rather than
quoting only the mean. What survives on an unlucky batch is the attempt reduction, which
never drops below −24% on any seed measured.

## 24. CI gates the invariant, reports the tuning outcome

**Decision.** The CI sweep fails the build if any seed shows a hard-stop case with a retry.
It prints the win rate but does not gate on it.

**Why.** Honouring hard stops is a correctness property; a legitimate policy change must
never move it. The size of the recovery delta is a tuning outcome; a legitimate policy
change might. Gating on the second would make CI punish honest experiments, and gating on
neither would let the one property that actually matters regress silently.

## 25. Actions record what they cost, in the audit event

**Decision.** Every `CHARGE_ATTEMPT` and `CONTACT_SENT` event carries
`inputs.action_cost_paise`. Total cost, and therefore net recovery, is summed from the log.

**Why.** The metrics contract in this project is that everything published is recomputable
from the audit trail alone. Reading channel prices out of `config/policies.yaml` at report
time would have broken that: the report would depend on the config file still holding the
prices that were in force when the run happened. Stamping the price onto the event at the
moment the money was spent keeps cost inside the same guarantee as every other number, and
means an old audit log stays correctly costed after the price list changes.

**Side effect.** The baseline is metered on the same basis, so "the naive strategy costs
more to run" is a measured statement rather than an assertion. A test asserts it.

## 26. `attempts_per_rupee_recovered` is kept, and paired with a readable form

The brief names that metric, so it is computed and published under that name. On a batch of
this size it renders as `0.0008`, which tells a reviewer nothing. `recovered_paise_per_attempt`
carries the same information the way a human reads it (₹1,334 recovered per charge attempt)
and both appear side by side. Renaming the one the brief asked for would have been worse.

## 27. The design is ablated, and the ablation is allowed to be unflattering

**Decision.** `reclaim ablate` disables one feature at a time (root-cause routing, curve-aware
timing, customer nudges, the cost floor) and re-measures over identical seeds. The report
renders the table and CI runs a six-seed version.

**Why.** The sweep answers "does it beat the baseline?". A reviewer's next question is "which
part of it is doing the work?", and prose is a weak answer. An ablation turns the project's
central claim, that routing by root cause is what earns the money, into a number: strip
routing and timing and the advantage over the naive baseline falls from +23.6% to +2.0%.

**The unflattering row is the point.** Disabling the cost floor changes recovery by
approximately zero. That is reported as approximately zero rather than smoothed away,
because the cost floor is a spend-control rule; it earns its place in the attempts column
(+1.9% attempts when removed), not the rupee column. A test asserts that a variant which
costs nothing is reported as costing nothing, so the table cannot quietly start flattering
the design later.

**Design constraint.** The "no root-cause routing" variant deliberately leaves hard stops
intact. Removing them too would have measured recklessness rather than routing, and would
have produced a much bigger, much less honest number. A test asserts hard stops survive
that variant.

**Cost.** Five full pipeline runs per seed makes this the slowest command in the project, at
about 15 seconds for 12 seeds. It is worth 15 seconds.

## 28. The README's own numbers are CI-verified

**Decision.** `reclaim verify-docs` takes every headline figure this project quotes, formats
it exactly as the README presents it, and checks the document contains it. CI regenerates the
seed-42 run, the sweep and the ablation, then runs it as a gate.

**Why.** The whole claim of this project is that its figures are measured rather than
asserted. A README that has drifted out of sync with the code undermines that claim more
effectively than a missing feature would: one stale number is enough for a reader to doubt
the ones that are correct, and they have no way to tell which is which.

**Design note.** It checks *presence* of the rendered figure rather than parsing the
Markdown. The property worth enforcing is "the number a reader sees is a number the pipeline
produced", not "the prose has a particular shape", and a parser would make ordinary editing
of the document fight the tool.

**Cost.** CI regenerates a 250-case run, a 30-seed sweep and a 12-seed ablation to have
something to check against, which adds about 25 seconds. A test also runs the check against
whatever artefacts happen to be on disk locally, and skips when there are none, so a fresh
checkout does not fail on it.

## 29. The escalation queue is written twice, on purpose

**Decision.** `out/escalations.jsonl` holds the most recent run, which is what the brief asks
for and what a human opens. `out/escalations_<run_id>.jsonl` is that run's permanent copy, and
`verify-audit`, `queue` and `report` all read the per-run file first.

**Why.** Originally there was only the shared file, so running a second batch silently
destroyed the first run's human work list. `verify-audit --run <older_id>` then failed with
"queue has 0 records, log has 34 ESCALATED events", and `queue --run <older_id>` returned
nothing. An audit trail that is append-only and permanent is worth very little if the work
list it produced is not. Found by adversarial review of the finished submission, not by a
test; there are now three regression tests, two of which fail without the fix.

## 30. A missing baseline log produces a notice, not a zero

**Decision.** When `out/audit_<run_id>-baseline.jsonl` is absent the report says so and drops
the comparison entirely: the delta card reads "not measured", the headline comparison section
is replaced by a statement that no claim is being made, and the three baseline columns
disappear from the per-category table.

**Why.** The report previously fell back to comparing the run against itself, which rendered
a confident-looking "+₹0.00" delta and a full table of zeroes. A reader has no way to
distinguish that from a real measurement of no improvement. On a page whose entire purpose is
measured claims, silently manufacturing a number out of a missing file is the worst available
behaviour. Also found by adversarial review, and covered by a test asserting the string
`+₹0.00` never appears in a baseline-less report.

## 31. The docs gate is anchored, mutation-tested, and honest about its limits

**Decision.** Each documented figure carries a context anchor that must appear in the same
table row, code line or paragraph. Figures that cannot be verified by a text search are
excluded rather than counted. A test mutates every checked figure and asserts the gate
notices. The README states precisely what the gate does and does not catch.

**Why.** The first version searched the whole document for a bare substring. Three of the
per-category checks rendered as `"0"`, which matches any document containing a zero, and six
more were two or three characters long. The gate reported 37 verified figures while at least
five of those checks could not fail. A gate that inflates its own coverage is worse than no
gate, because it converts an unexamined assumption into a false assurance.

**Design note.** Anchoring on the *line* was the obvious fix and the wrong one: prose wraps,
so a figure and the words that give it meaning routinely sit on different lines. Anchoring on
the whole document makes every anchor vacuous. The right unit is the Markdown row or
paragraph, so `segment()` splits table rows and fenced-code lines out individually and joins
wrapped prose back into paragraphs. Reflowing a sentence no longer fights the tool.

**Limit, stated rather than glossed.** Mutating every occurrence of a figure is caught 34
times out of 34. Mutating a single occurrence is caught 30 times out of 34, because four
figures are quoted in more than one place and `"30"` appears twelve times in the README. So
the gate catches a stale number; it does not catch a partial edit that fixed one mention and
missed another. The README says this in those words, because an overstated claim about a
verification tool is precisely the failure the tool exists to prevent.

## 32. An unrecorded cost is unknown, not zero

**Decision.** `RunMetrics.action_cost_recorded` is false when the log contains billable
actions carrying no cost stamp. The CLI then prints "not recorded in this log, so no net
figure is claimed" and the report renders "not recorded" instead of a rupee value.

**Why.** Summing a missing field to zero published `cost of acting: Rs 0.00` and a net figure
equal to the gross one. An audit log written before cost tracking existed is indistinguishable
in that presentation from a run that genuinely cost nothing, which is a fabricated measurement
rather than a missing one. Found by deliberately grepping for this defect shape after hitting
it three times; see `CHALLENGES.md` §14.

**Design note.** The alternative was to make the field required and refuse to read older logs.
That trades a false number for a broken tool, which is worse: an old audit log is still a
valid record of what happened, it simply does not know what the actions cost. Saying so is
the correct behaviour.

## 33. Defaults chosen where the brief was silent

| Choice | Value | Reasoning |
|---|---|---|
| Batch size default | 250 | As specified; floor of 50 enforced in code. |
| Recovery horizon | 21 days | Longer than the longest plan (170h) with room for deferrals. |
| Currency | INR only | The brief specifies INR. Amounts are integer paise throughout; no float ever touches money. |
| Amounts | Rounded to whole rupees | Real subscription debits are not sub-rupee. |
| Customers per batch | ~size/3 | Multiple failed cases per customer, so the per-customer contact cap actually binds. |
| Report format | Single self-contained HTML file | No external requests. A test asserts the rendered file contains no `http://` or `https://`. |
| Escalation queue file | Overwritten per run, records carry `run_id` | The queue is a work list, not an archive. The archive is the audit log. |
