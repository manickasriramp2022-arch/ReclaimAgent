# Compliance notes

Every regulatory and network constraint ReclaimAgent enforces lives in
[`config/compliance.yaml`](config/compliance.yaml) as a named, editable constant with an
explicit provenance field. There are no compliance figures hardcoded in Python and none
asserted as fact in code comments.

Read this document before changing any value in that file.

## The honesty rule this project follows

A payment-recovery system that invents a regulation is worse than one that admits it does
not know the number. So each constant carries four fields:

| Field | Meaning |
|---|---|
| `value` | The number or flag the engine actually enforces. |
| `unverified` | `true` when the author could not verify the figure against a primary source while building this project. |
| `source` | Where a production team must go to confirm it. |
| `note` | What the constraint is for, and what the engine does when it binds. |

**Nine constants are marked `unverified: true`.** They are reproduced in full below. Every
one of them is a placeholder that behaves correctly as a mechanism and carries a number that
has not been confirmed. A test in `tests/test_compliance.py` asserts that every constant
marked unverified also names a source containing the word `CONFIRM`, so a future edit cannot
quietly drop the provenance.

## How the engine uses these constants

The compliance layer answers one question per proposed action, and returns one of three
verdicts:

- **Allow.** Every precondition is met.
- **Refuse.** A precondition cannot be met. The refusal is written to the audit log as a
  `COMPLIANCE_REFUSAL` event naming the constant. **A refusal is not a failed recovery
  attempt.** Cases refused on the charge rail are subtracted from the value at risk to give
  the addressable value, and the headline recovery rate is measured against that. The gross
  rate is reported next to it, so nothing is hidden by the choice of denominator.
- **Defer.** The precondition will be met later on its own — a quiet-hours window that
  closes, a pre-debit notification lead time that elapses, a retry-spacing gap that passes.
  The action is rescheduled rather than cancelled, within a bounded deferral budget.

Two distinctions the layer draws deliberately, because collapsing either one produces wrong
behaviour:

1. **Consent gates customer contact, not the charge rail.** Re-presenting an existing
   authorisation is a different permission from messaging a customer. An earlier build
   terminated the whole case when contact consent was missing, which silently stranded
   recoverable money on 20 cases in a 250-case batch. See `CHALLENGES.md`.
2. **A transaction that never reached an authorisation decision is not a declined
   authorisation.** Card-network retry-spacing rules are written for declines. A gateway
   timeout returned no issuer decision, so `card_network.retry_spacing_exempt_categories`
   excludes `TECHNICAL_ERROR` from the spacing rule and lets the policy's own short backoff
   govern. That exemption is itself marked unverified.

---

# Values requiring confirmation before production use

The nine constants below are marked `unverified: true` in `config/compliance.yaml`. Each is
a best-effort placeholder. **None of them should be relied on in production without being
confirmed against the primary source named, with compliance counsel.**

### 1. `emandate.pre_debit_notification_lead_hours`

- **Value in config:** `24` hours
- **Enforces:** A recurring debit is refused unless a pre-debit notification was sent at
  least this many hours before the debit instant. Refused if no notification exists at all;
  deferred if one exists but the window has not yet elapsed.
- **Confirm against:** RBI's framework for processing e-mandates on recurring transactions
  (RBI/2019-20/47, DPSS.CO.PD.No.447/02.14.003/2019-20, and subsequent circulars).
- **What specifically needs confirming:** the current required lead time, and the exact
  content and delivery requirements for the notification itself, which this project does not
  model at all.

### 2. `emandate.afa_exemption_threshold_paise`

- **Value in config:** `1500000` paise (₹15,000)
- **Enforces:** Above this amount, an unattended auto-debit retry is refused and the case is
  routed to a customer-authenticated path instead.
- **Confirm against:** RBI circulars on Additional Factor of Authentication for recurring
  e-mandate transactions.
- **What specifically needs confirming:** the threshold has been revised more than once and
  differs by transaction category. Both the current applicable figure and the category
  carve-outs need checking. This is the single value most likely to be stale.

### 3. `emandate.max_debits_per_mandate_per_day`

- **Value in config:** `1`
- **Enforces:** A rolling 24-hour cap on debit attempts against a single mandate.
- **Confirm against:** network and sponsor-bank operating rules for NACH and card-on-file
  e-mandates.
- **What specifically needs confirming:** whether a per-mandate daily cap exists at all in
  the relevant scheme, and if so its value.

### 4. `card_network.max_retries_per_declined_authorisation`

- **Value in config:** `4`
- **Enforces:** A hard ceiling on retry attempts per originally-declined authorisation,
  counting attempts made before the batch, applied on top of the per-policy attempt cap.
- **Confirm against:** Visa and Mastercard merchant retry rules for declined authorisations.
- **What specifically needs confirming:** these caps differ by network, by decline reason
  code and by region, and they change. A single global number is a simplification. A
  production system needs a per-network, per-reason-code table.

### 5. `card_network.min_hours_between_retries`

- **Value in config:** `24` hours
- **Enforces:** Minimum spacing between two retry attempts on the same declined
  authorisation. Produces a deferral, not a refusal.
- **Confirm against:** Visa and Mastercard merchant retry rules.
- **What specifically needs confirming:** as above, this varies by network and reason code.

### 6. `card_network.retry_spacing_exempt_categories`

- **Value in config:** `["TECHNICAL_ERROR"]`
- **Enforces:** Root causes excluded from `card_network.min_hours_between_retries`, on the
  reasoning that a gateway timeout or upstream 5xx never produced an issuer decision and so
  is not a declined authorisation.
- **Confirm against:** the acquirer's own mapping of response codes.
- **What specifically needs confirming:** which acquirer response codes genuinely fall
  outside the retry-spacing rules. The reasoning is sound; the mapping is not verified.

### 7. `contact.quiet_hours_local`

- **Value in config:** `21:00`–`09:00` `Asia/Kolkata`
- **Enforces:** No outbound customer contact inside this local window. Produces a deferral
  to the next permitted instant.
- **Confirm against:** TRAI Telecom Commercial Communications Customer Preference
  Regulations.
- **What specifically needs confirming:** the exact permitted time band, and — importantly —
  the carve-outs for transactional and service messages, which a payment-failure notice may
  well fall under. If it does, this rule is stricter than required, which is the safe
  direction to be wrong in but still wrong.

### 8. `contact.max_contacts_per_customer_per_day`

- **Value in config:** `2`
- **Enforces:** Rolling 24-hour cap on outbound dunning messages per customer across all
  channels.
- **Confirm against:** TRAI commercial-communication frequency norms, plus merchant policy.
- **What specifically needs confirming:** whether a regulatory frequency cap applies to this
  message class at all, versus this being purely a merchant policy choice.

### 9. `contact.max_contacts_per_customer_per_week`

- **Value in config:** `5`
- **Enforces:** Rolling 7-day cap on outbound dunning messages per customer.
- **Confirm against:** merchant policy. This one is openly a placeholder with no regulatory
  claim behind it.
- **What specifically needs confirming:** the merchant's own contact policy.

---

## Constants that are *not* marked unverified, and why

These are design decisions or first-principles constraints, not regulatory figures. They are
listed here so the distinction is explicit.

| Constant | Why it is not flagged |
|---|---|
| `consent.required_before_contact` | A design decision to consent-gate outbound dunning. Defensible under India's DPDP Act 2023 and TRAI rules, but stated as a choice, not as a citation. |
| `consent.refusal_is_terminal_for_contact_channels` | A design decision. Missing consent is not something a retry loop can cure. |
| `emandate.mandate_must_be_active` | First principles and contract: a revoked, paused or expired mandate is not a valid debit authority. No citation needed to refuse it. |
| `card_network.no_retry_reason_codes` | Derived from this project's own hard-stop categories. A production system must map it to its acquirer's real reason codes. |
| `data.environment_must_be_test` | A design decision. This project refuses to process anything not marked `environment: test`. |
| `data.log_pan_or_vpa` | No card number, VPA or contact identifier is generated or logged anywhere in this project. Only opaque customer and mandate identifiers reach the audit trail. |

## Data handling

- No PAN, VPA, phone number or email address is generated, stored or logged. Customers are
  opaque identifiers (`cust_00042`), mandates likewise (`mdt_42_00007`).
- Contact channel availability is recorded as the strings `email`, `sms`, `whatsapp` — the
  existence of a channel, never an address on it.
- Every generated record is marked `environment: test`, and `FailedTransaction` raises on
  construction if it is not, so a live record cannot enter the pipeline even by accident.
- `ANTHROPIC_API_KEY` is read from the environment only. `.env` is git-ignored, `.env.example`
  ships a placeholder, and CI greps the tree for anything resembling a committed key.
