# Demo runsheet — 5 minutes

Target: 5:00. Budget below sums to 5:00 with ~15s of slack. Everything runs offline with
`--no-llm`, so nothing in this script can fail because of a network or model problem.

## Before you record

```bash
make install
reclaim generate --seed 42 --size 250
reclaim run --batch data/batch_42.jsonl --no-llm
reclaim report
```

Then `rm -rf out/* data/*.jsonl` so the live run is genuinely live, and keep the rendered
report open in a second browser tab as a fallback if anything goes wrong on camera.

Terminal at ~110 columns, large font. Have four commands in shell history, in order:

```bash
reclaim generate --seed 42 --size 250
reclaim run --batch data/batch_42.jsonl --no-llm
reclaim replay --case case_42_00197
reclaim replay --case @stopped
reclaim benchmark --seeds 30
```

---

## 0:00–0:30 — The problem (30s)

*On screen: `README.md`, the "The problem" section.*

> A failed recurring payment is not one problem, it's six, and they want opposite things
> from you.
>
> A customer whose balance ran dry wants you to wait and retry after payday. An issuer soft
> decline wants a fast retry and then a fast stop. An expired card can't be recovered by
> retrying at all — only the customer can fix it. A stolen card or a revoked mandate must
> never be retried, because every further attempt is at best wasted and at worst an
> unauthorised debit.
>
> Most dunning systems collapse all six into one loop: retry three times, a day apart, give
> up. That loop burns attempts on cases it can never win, re-presents at the worst moment on
> the ones it could, and leaves nobody able to answer "why did we charge this card four
> times?"
>
> ReclaimAgent routes each failure by root cause, bounds every path with named stopping
> rules, refuses to act when a compliance precondition isn't met, and writes every decision
> to an append-only audit trail. Then it proves the numbers from that trail.

## 0:30–1:30 — Architecture (60s)

*On screen: the mermaid diagram in `README.md`.*

> Five stages. **Ingest** a batch of synthetic, test-mode failed transactions.
>
> **Classify** by root cause — a deterministic rule layer over decline codes first. Only
> codes no rule matches reach an LLM, and the model is pinned to a closed set of seven
> categories by a tool schema, with a confidence floor underneath it. Below the floor, the
> answer is thrown away and the case becomes `UNKNOWN`, which escalates. The model can't
> invent a category and nothing gets silently retried on a low-confidence guess.
>
> **Decide** — one declarative plan per category in `policies.yaml`, then every stopping
> rule: hard stop, attempt cap, rolling window, cost floor, contact cap, batch circuit
> breaker. Then the compliance gate: consent, mandate state, AFA threshold, pre-debit
> notification window, quiet hours, network retry caps. All named constants in
> `compliance.yaml`, and every one I couldn't verify against a primary source is flagged
> `unverified` and listed in `COMPLIANCE_NOTES.md`.
>
> **Act** — outcomes are simulated, seeded, and I'll say that again on every screen where a
> rupee figure appears.
>
> And every arrow into that box on the right is a write to the **audit trail**. Nothing
> decides anything without recording who decided, what it consulted, what it concluded, and
> how many rupees were at stake.

## 1:30–3:30 — Live run and report (120s)

*Type it. Don't paste.*

```bash
reclaim generate --seed 42 --size 250
```

> 250 synthetic failed transactions, ₹9.83 lakh at risk. Skewed, not uniform — insufficient
> funds and soft declines dominate, hard declines and revoked mandates are a minority.
> That mix is one config table, and every record is marked `environment: test`. The engine
> refuses to process anything that isn't.

```bash
reclaim run --batch data/batch_42.jsonl --no-llm
```

*Runs in about a second. Let the output sit.*

> ₹513,660 recovered, 101 of 250 cases, on 385 charge attempts.
>
> Second line down: ₹149,917 the compliance layer refused to touch. That's removed from the
> denominator, because a refusal is not a failed recovery.
>
> **22 out of 22 hard stops honoured with zero retries.** Not "we tried not to" — zero, and
> I'll prove it from the log in a moment.
>
> 115 cases escalated to a human, ₹428,843 sitting in that queue, each one with its full
> decision chain and a recommended action attached.

*Point at the like-for-like block.*

> Against the naive baseline — retry everything three times, 24 hours apart, identical
> batch, identical seed, identical simulator — **plus ₹32,042 recovered on 169 fewer charge
> attempts**. Same money, less network wear.
>
> And the line underneath is the one I want you to notice. Across the *whole* batch the
> naive baseline recovers more than I do. It gets ₹71,084 by debiting revoked mandates and
> skipping pre-debit notification windows — 85 debits this system had already refused to
> make. That's not a win and I'm not netting it off. It's in the report, with its cause
> named.

```bash
reclaim report && open out/report_*.html
```

*Scroll: headline cards, the caveat paragraph, the per-category table.*

> Three zeros in the attempts column: hard decline, revoked mandate, unknown. Zero
> attempts, every time.

## 3:30–4:30 — Replay (60s)

```bash
reclaim replay --case case_42_00197
```

> One case, end to end, straight out of the audit log. ₹8,926, card expired.
>
> Classified by the rule layer on an exact code match. Policy selected: retrying an expired
> instrument cannot succeed, so lead with the update-payment-method link and re-present
> exactly once, late.
>
> Then look at sequence 861: the link was due at 03:00 UTC, which is 08:30 in India, inside
> quiet hours. **Deferred, not cancelled** — rescheduled to 09:00 local, and sent at 900.
> The customer acted. That uplift is why the single re-presentment four days later goes out
> at probability 0.49 instead of 0.07, and it's authorised. ₹8,926 recovered on one attempt.

```bash
reclaim replay --case @stopped
```

> And the opposite. ₹11,285, account closed. Ingested, classified, policy selected, stopped.
> **Four events. Zero attempts.** The policy's own words are in the log: this is a statement
> about authority, not about timing. Re-presenting burns network goodwill and can attract
> penalties.
>
> That's the case a naive retry loop charges three more times.

## 4:30–5:00 — Does it hold, and can you prove it? (30s)

```bash
reclaim benchmark --seeds 30
```

> Before the proof, the obvious objection: is that delta real, or did I pick a good seed?
> Thirty independently generated batches, eight seconds. **Thirty out of thirty recover
> more.** Median plus eleven percent. Worst seed plus zero point seven, which I'm showing
> you rather than hiding. And the seed I've been quoting all the way through this demo, 42,
> comes in at plus six point seven — *below* the median. The headline is a below-average
> case.
>
> Bottom row: hard stops honoured on every seed. That one isn't a tuning result, it's an
> invariant, so CI sweeps twelve seeds on every push and fails the build if a single one of
> them ever retries a stolen card.
>
> One more line on that table, because it doesn't flatter me. The recovery *rate* on this
> batch is sixty-two percent. Across the thirty seeds the median is forty-five, and a
> ten-thousand-case batch comes in at forty. So seed forty-two is a below-median sample of
> the delta and an above-median sample of the rate. If you want one number for how much of
> the addressable value this recovers, take forty-five percent, not sixty-two. Both spreads
> are in the report because reporting only the one that made me look good would be the exact
> failure this project is built to avoid.

*If you have 15 seconds spare, show the ablation table already in the open report instead of
re-running it.*

> And the next question is which part of the design is doing that. Disable one feature at a
> time: root-cause routing is worth eighteen percent of recovery, timing against each cause's
> curve another sixteen, nudges eight. Take routing and timing away and the whole advantage
> over the naive baseline collapses from twenty-four percent to two. That's the thesis of the
> project as a number.
>
> Last row: removing the cost floor changes recovery by zero. I left that in. The cost floor
> is a spend-control rule, so it shows up in the attempts column, not the rupees column. A
> table built to flatter the design wouldn't have that row in it.

```bash
reclaim verify-audit
```

> This throws away every number I just showed you and recomputes it from the JSONL alone.
> Sequence continuity, SHA-256 hash chain, no attempt on any hard-stop or unclassified case,
> every stop and refusal naming the rule that fired, and every field of the reported metrics
> re-derived from the log and diffed. **If a number in that report can't be reproduced from
> the audit trail, this command fails.** It runs in CI.
>
> So: **plus ₹32,042 on 169 fewer attempts, and it holds on 30 out of 30 seeds**, 100% of
> hard stops honoured with zero retries,
> 83 compliance refusals itemised by rule, 115 cases escalated with reasoning attached, and
> every figure recomputable from an append-only log.
>
> Outcomes are simulated. The measurement discipline is not.

---

## Fallbacks

| If | Do |
|---|---|
| A command hangs | `Ctrl-C`, use the pre-rendered report in the second tab. Every number in this script is already in it. |
| `@stopped` picks a different case | Any hard decline works. The line is always "four events, zero attempts". |
| You are running long | Cut the second `replay`. The report's second worked example shows the same thing. Never cut the sweep — it is the difference between an anecdote and a measurement. |
| Asked "how many seeds did you try before 42?" | "Forty-two was the first and only one used while building. The sweep answers it properly: thirty out of thirty win, and forty-two is below the median of that distribution." |
| Asked "are these real numbers?" | "No, and the report says so on its face. Outcomes come from a seeded model in `simulation.yaml`. What's real is that every figure recomputes from the audit log, and that the policy engine can't see the simulator when it decides." |
| Asked "so what recovery rate should we expect?" | "Forty-five percent of addressable value, the median across thirty seeds. Not the sixty-two on screen, which is a high sample. The report shows the whole range, 23 to 65." |
| Asked "why is the rate so variable?" | "Two hundred and fifty cases is a small sample and the amounts are skewed, so a couple of large recoveries move the rate a lot. The delta against the baseline is much steadier: thirty of thirty seeds, and the attempt reduction never drops below 24 percent." |
| Asked about the RBI figures | "Flagged `unverified` in `compliance.yaml` and listed in `COMPLIANCE_NOTES.md` with the circular to check. I'd rather show you the mechanism and admit the constant needs confirming than quote a number I can't source." |
