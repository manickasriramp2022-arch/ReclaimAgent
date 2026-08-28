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

## 23. Defaults chosen where the brief was silent

| Choice | Value | Reasoning |
|---|---|---|
| Batch size default | 250 | As specified; floor of 50 enforced in code. |
| Recovery horizon | 21 days | Longer than the longest plan (170h) with room for deferrals. |
| Currency | INR only | The brief specifies INR. Amounts are integer paise throughout; no float ever touches money. |
| Amounts | Rounded to whole rupees | Real subscription debits are not sub-rupee. |
| Customers per batch | ~size/3 | Multiple failed cases per customer, so the per-customer contact cap actually binds. |
| Report format | Single self-contained HTML file | No external requests. A test asserts the rendered file contains no `http://` or `https://`. |
| Escalation queue file | Overwritten per run, records carry `run_id` | The queue is a work list, not an archive. The archive is the audit log. |
