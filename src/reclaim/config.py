"""Typed loaders for the four YAML configuration files.

Configuration is validated into Pydantic models at load time, so a malformed
policy file fails loudly at startup rather than silently changing behaviour
halfway through a batch.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import CategoryPolicy, Channel, RootCause, StepPlan

CONFIG_DIR_ENV = "RECLAIM_CONFIG_DIR"


def default_config_dir() -> Path:
    """Locate config/ relative to the installed package or the repo root."""
    import os

    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config"
        if (candidate / "policies.yaml").is_file():
            return candidate
    raise FileNotFoundError("could not locate a config/ directory containing policies.yaml")


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


class Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Loose(BaseModel):
    """For config blocks whose extra keys are documentation, not behaviour."""

    model_config = ConfigDict(frozen=True, extra="allow")


# --------------------------------------------------------------------------
# generator.yaml
# --------------------------------------------------------------------------
class AmountBand(Strict):
    name: str
    weight: float = Field(gt=0)
    min_paise: int = Field(gt=0)
    max_paise: int = Field(gt=0)


class DeclineSpec(Strict):
    code: str
    description: str
    weight: float = Field(gt=0)
    methods: list[str]


class GeneratorConfig(Strict):
    currency: str
    environment: str
    amount_bands: list[AmountBand]
    payment_method_mix: dict[str, float]
    decline_mix: list[DeclineSpec]
    contact_channel_availability: dict[str, float]
    contact_consent_probability: float
    prior_attempt_distribution: dict[int, float]
    mandate_status_mix: dict[str, float]
    attempt_window_hours: int
    batch_anchor_utc: datetime
    pre_debit_notice_sent_probability: float


# --------------------------------------------------------------------------
# compliance.yaml
# --------------------------------------------------------------------------
class ComplianceConstant(Loose):
    """A named constant with provenance. `value` may be a number, bool or list."""

    value: Any = None
    unverified: bool = False
    source: str = ""
    note: str = ""


class QuietHours(Loose):
    start: str
    end: str
    timezone: str
    unverified: bool = False
    source: str = ""
    note: str = ""

    def window(self) -> tuple[time, time]:
        sh, sm = (int(x) for x in self.start.split(":"))
        eh, em = (int(x) for x in self.end.split(":"))
        return time(sh, sm), time(eh, em)


class ContactCompliance(Strict):
    quiet_hours_local: QuietHours
    max_contacts_per_customer_per_day: ComplianceConstant
    max_contacts_per_customer_per_week: ComplianceConstant


class ComplianceConfig(Strict):
    schema_version: int
    consent: dict[str, ComplianceConstant]
    emandate: dict[str, ComplianceConstant]
    card_network: dict[str, ComplianceConstant]
    contact: ContactCompliance
    data: dict[str, ComplianceConstant]

    def unverified_entries(self) -> list[tuple[str, ComplianceConstant | QuietHours]]:
        found: list[tuple[str, ComplianceConstant | QuietHours]] = []
        for section in ("consent", "emandate", "card_network", "data"):
            for name, const in getattr(self, section).items():
                if const.unverified:
                    found.append((f"{section}.{name}", const))
        if self.contact.quiet_hours_local.unverified:
            found.append(("contact.quiet_hours_local", self.contact.quiet_hours_local))
        for name in ("max_contacts_per_customer_per_day", "max_contacts_per_customer_per_week"):
            const = getattr(self.contact, name)
            if const.unverified:
                found.append((f"contact.{name}", const))
        return found

    # Convenience accessors, so the engine never index-chases raw dicts.
    @property
    def consent_required(self) -> bool:
        return bool(self.consent["required_before_contact"].value)

    @property
    def pre_debit_lead_hours(self) -> float:
        return float(self.emandate["pre_debit_notification_lead_hours"].value)

    @property
    def afa_threshold_paise(self) -> int:
        return int(self.emandate["afa_exemption_threshold_paise"].value)

    @property
    def mandate_must_be_active(self) -> bool:
        return bool(self.emandate["mandate_must_be_active"].value)

    @property
    def max_debits_per_mandate_per_day(self) -> int:
        return int(self.emandate["max_debits_per_mandate_per_day"].value)

    @property
    def network_max_retries(self) -> int:
        return int(self.card_network["max_retries_per_declined_authorisation"].value)

    @property
    def network_min_hours_between_retries(self) -> float:
        return float(self.card_network["min_hours_between_retries"].value)

    @property
    def retry_spacing_exempt_categories(self) -> frozenset[str]:
        return frozenset(self.card_network["retry_spacing_exempt_categories"].value or [])

    @property
    def no_retry_reason_codes(self) -> frozenset[str]:
        return frozenset(self.card_network["no_retry_reason_codes"].value or [])

    @property
    def max_contacts_per_day(self) -> int:
        return int(self.contact.max_contacts_per_customer_per_day.value)

    @property
    def max_contacts_per_week(self) -> int:
        return int(self.contact.max_contacts_per_customer_per_week.value)

    @property
    def environment_must_be_test(self) -> bool:
        return bool(self.data["environment_must_be_test"].value)


# --------------------------------------------------------------------------
# policies.yaml
# --------------------------------------------------------------------------
class PolicyDefaults(Strict):
    escalate_on_exhaustion: bool
    escalate_on_terminal_refusal: bool
    escalate_terminal_above_paise: int


class CostFloorRule(Loose):
    enabled: bool
    description: str = ""
    attempt_cost_paise: dict[str, int]
    min_expected_value_multiple: float
    expected_success_prior: dict[str, list[float]]
    recovered_value_margin: float

    def cost_of(self, channel: Channel) -> int:
        return int(self.attempt_cost_paise[str(channel)])

    def prior(self, category: RootCause, attempt_index: int) -> float:
        table = self.expected_success_prior.get(str(category), [0.0])
        idx = max(0, min(attempt_index - 1, len(table) - 1))
        return float(table[idx])


class RollingWindowRule(Loose):
    enabled: bool
    window_hours: float
    max_charge_attempts: int
    description: str = ""
    exempt_categories: list[str] = Field(default_factory=list)


class QuietHoursRule(Loose):
    enabled: bool
    description: str = ""
    max_deferrals_per_step: int


class CircuitBreakerRule(Loose):
    enabled: bool
    description: str = ""
    min_attempts_before_arming: int
    failure_rate_threshold: float
    expected_shortfall_ratio: float = 0.25
    min_expected_successes_in_window: float = 5.0
    window_attempts: int


class SimpleRule(Loose):
    enabled: bool
    description: str = ""


class StoppingRules(Strict):
    hard_stop_category: Loose
    unknown_requires_human: Loose
    max_attempts_per_case: Loose
    rolling_window_attempt_cap: RollingWindowRule
    cost_floor: CostFloorRule
    contact_frequency_cap: SimpleRule
    quiet_hours: QuietHoursRule
    plan_exhausted: SimpleRule
    network_retry_cap: SimpleRule
    batch_circuit_breaker: CircuitBreakerRule


class BaselineConfig(Strict):
    name: str
    description: str
    attempts: int
    interval_hours: float
    ignores_compliance: bool
    ignores_hard_stops: bool


class EscalationConfig(Strict):
    recoverability_weight: dict[str, float]
    recommended_actions: dict[str, str]

    def weight(self, category: RootCause) -> float:
        return float(self.recoverability_weight.get(str(category), 0.3))

    def action_for(self, rule: str, decline_code: str) -> str:
        template = self.recommended_actions.get(rule) or self.recommended_actions["default"]
        return template.replace("{decline_code}", decline_code)


class PolicyConfig(Strict):
    schema_version: int
    defaults: PolicyDefaults
    policies: dict[str, CategoryPolicy]
    stopping_rules: StoppingRules
    baseline: BaselineConfig
    escalation: EscalationConfig

    def for_category(self, category: RootCause) -> CategoryPolicy:
        policy = self.policies.get(str(category))
        if policy is None:
            raise KeyError(f"no policy configured for category {category}")
        return policy


# --------------------------------------------------------------------------
# simulation.yaml
# --------------------------------------------------------------------------
class TimeFactor(Loose):
    kind: str
    factor: float | None = None
    points: list[dict[str, float]] | None = None

    def at(self, hours: float) -> float:
        if self.kind == "constant":
            return float(self.factor or 0.0)
        pts = self.points or []
        if not pts:
            return 1.0
        if hours <= pts[0]["hours"]:
            return float(pts[0]["factor"])
        for left, right in zip(pts, pts[1:], strict=False):
            if left["hours"] <= hours <= right["hours"]:
                span = right["hours"] - left["hours"]
                if span <= 0:
                    return float(right["factor"])
                frac = (hours - left["hours"]) / span
                return float(left["factor"] + frac * (right["factor"] - left["factor"]))
        return float(pts[-1]["factor"])


class PaydayBonus(Strict):
    days_of_month: list[int]
    factor: float


class CategorySim(Loose):
    base: float
    decay: float
    time_factor: TimeFactor
    payday_bonus: PaydayBonus | None = None


class Clamp(Strict):
    floor: float
    ceiling: float


class ContactUplift(Strict):
    default: float
    by_category: dict[str, float] = Field(default_factory=dict)

    def for_category(self, category: RootCause) -> float:
        return float(self.by_category.get(str(category), self.default))


class SimulationConfig(Strict):
    schema_version: int
    clamp: Clamp
    categories: dict[str, CategorySim]
    contact_response: dict[str, float]
    contact_uplift: ContactUplift


# --------------------------------------------------------------------------
# decline_rules.yaml
# --------------------------------------------------------------------------
class ExactRule(Strict):
    category: RootCause
    rule_id: str


class PatternRule(Strict):
    regex: str
    category: RootCause
    rule_id: str


class KeywordRule(Strict):
    any_of: list[str]
    category: RootCause
    rule_id: str


class LlmFallbackConfig(Strict):
    min_confidence: float
    model: str
    max_tokens: int
    cache_enabled: bool
    cache_path: str


class DeclineRuleConfig(Strict):
    schema_version: int
    exact: dict[str, ExactRule]
    patterns: list[PatternRule]
    keywords: list[KeywordRule]
    llm_fallback: LlmFallbackConfig


# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------
class AppConfig(Strict):
    generator: GeneratorConfig
    compliance: ComplianceConfig
    policies: PolicyConfig
    simulation: SimulationConfig
    decline_rules: DeclineRuleConfig
    fingerprint: str
    config_dir: Path

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


# Keys a policy block may contain. Anything else is a typo, and a typo here
# silently changes the policy the system claims to be executing: writing `plann`
# instead of `plan` leaves a recoverable category with no steps at all, and the
# run still exits 0 while recovering nothing from it.
POLICY_KEYS: frozenset[str] = frozenset(
    {
        "recoverable",
        "immediate_terminal",
        "terminal_rule",
        "max_charge_attempts",
        "backoff_hours",
        "allowed_channels",
        "quiet_hours_apply",
        "plan",
        "terminal_conditions",
        "always_escalate",
        "rationale",
    }
)


def _closest_key(unknown: str) -> str | None:
    """Best guess at what a mistyped key was meant to be."""
    import difflib

    matches = difflib.get_close_matches(unknown, sorted(POLICY_KEYS), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _coerce_policies(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise the policies block into CategoryPolicy-shaped mappings.

    Unknown keys are rejected rather than ignored. Silently dropping one turns a
    misspelling into a policy change that no test, metric or audit event would
    reveal.
    """
    out: dict[str, Any] = {}
    for name, block in raw.items():
        category = RootCause(name)
        unknown = sorted(set(block) - POLICY_KEYS)
        if unknown:
            hints = []
            for key in unknown:
                near = _closest_key(key)
                hints.append(f"{key!r}" + (f" (did you mean {near!r}?)" if near else ""))
            raise ValueError(
                f"policies.yaml: policy {name} has unrecognised key(s): {', '.join(hints)}. "
                "A misspelled key would silently change this policy's behaviour, so it is "
                "rejected rather than ignored."
            )
        missing = {
            "recoverable",
            "immediate_terminal",
            "max_charge_attempts",
            "quiet_hours_apply",
        } - set(block)
        if missing:
            raise ValueError(
                f"policies.yaml: policy {name} is missing required key(s): "
                f"{', '.join(sorted(missing))}"
            )
        plan = [
            StepPlan(after_hours=float(s["after_hours"]), channel=Channel(s["channel"]))
            for s in block.get("plan", [])
        ]
        out[name] = CategoryPolicy(
            category=category,
            recoverable=bool(block["recoverable"]),
            immediate_terminal=bool(block["immediate_terminal"]),
            terminal_rule=block.get("terminal_rule"),
            max_charge_attempts=int(block["max_charge_attempts"]),
            backoff_hours=[float(h) for h in block.get("backoff_hours", [])],
            allowed_channels=[Channel(c) for c in block.get("allowed_channels", [])],
            quiet_hours_apply=bool(block["quiet_hours_apply"]),
            plan=plan,
            terminal_conditions=list(block.get("terminal_conditions", [])),
            always_escalate=bool(block.get("always_escalate", False)),
            rationale=str(block.get("rationale", "")).strip(),
        )
    missing = {str(c) for c in RootCause} - set(out)
    if missing:
        raise ValueError(f"policies.yaml is missing a policy for: {sorted(missing)}")
    return out


def load_config(config_dir: Path | None = None) -> AppConfig:
    cdir = config_dir or default_config_dir()
    files = {
        "generator": cdir / "generator.yaml",
        "compliance": cdir / "compliance.yaml",
        "policies": cdir / "policies.yaml",
        "simulation": cdir / "simulation.yaml",
        "decline_rules": cdir / "decline_rules.yaml",
    }
    raw = {name: _read_yaml(path) for name, path in files.items()}

    hasher = hashlib.sha256()
    for name in sorted(files):
        hasher.update(files[name].read_bytes())

    pol_raw = dict(raw["policies"])
    pol_raw["policies"] = _coerce_policies(pol_raw["policies"])

    return AppConfig(
        generator=GeneratorConfig.model_validate(raw["generator"]),
        compliance=ComplianceConfig.model_validate(raw["compliance"]),
        policies=PolicyConfig.model_validate(pol_raw),
        simulation=SimulationConfig.model_validate(raw["simulation"]),
        decline_rules=DeclineRuleConfig.model_validate(raw["decline_rules"]),
        fingerprint=hasher.hexdigest()[:16],
        config_dir=cdir,
    )


@lru_cache(maxsize=4)
def cached_config(config_dir_str: str | None = None) -> AppConfig:
    return load_config(Path(config_dir_str) if config_dir_str else None)
