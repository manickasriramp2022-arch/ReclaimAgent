"""Component 5: the outcome simulator.

NOTHING HERE TOUCHES A PAYMENT GATEWAY. Every outcome this module returns is
drawn from the probabilistic model in config/simulation.yaml. These are
simulated results and the report says so on its face.

The model is deliberately kept separate from the engine's own beliefs in
config/policies.yaml (`cost_floor.expected_success_prior`). The engine decides
whether an attempt is worth making using its prior; the simulator decides what
actually happens. The engine never reads this file, so the cost-floor stopping
rule is being tested against a world it cannot see.

Determinism: each draw uses an RNG seeded from
(batch_seed, case_id, attempt_index, kind), so reruns reproduce every outcome.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .config import SimulationConfig
from .models import Channel, RootCause


class AttemptOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    probability: float
    roll: float
    hours_since_original: float
    contact_uplift_applied: bool


class ContactOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_acted: bool
    probability: float
    roll: float


def _rng(batch_seed: int, case_id: str, index: int, kind: str) -> random.Random:
    key = f"{batch_seed}|{case_id}|{index}|{kind}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))


class OutcomeSimulator:
    def __init__(self, cfg: SimulationConfig, batch_seed: int) -> None:
        self.cfg = cfg
        self.batch_seed = batch_seed

    def success_probability(
        self,
        category: RootCause,
        attempt_index: int,
        hours_since_original: float,
        instant: datetime,
        customer_acted: bool,
    ) -> float:
        sim = self.cfg.categories.get(str(category))
        if sim is None:
            return 0.0
        p = sim.base * (sim.decay ** max(0, attempt_index - 1))
        p *= sim.time_factor.at(max(0.0, hours_since_original))
        if sim.payday_bonus is not None and instant.day in sim.payday_bonus.days_of_month:
            p *= sim.payday_bonus.factor
        if customer_acted:
            p *= self.cfg.contact_uplift.for_category(category)
        return min(max(p, self.cfg.clamp.floor), self.cfg.clamp.ceiling)

    def attempt(
        self,
        case_id: str,
        category: RootCause,
        attempt_index: int,
        hours_since_original: float,
        instant: datetime,
        customer_acted: bool,
    ) -> AttemptOutcome:
        p = self.success_probability(
            category, attempt_index, hours_since_original, instant, customer_acted
        )
        roll = _rng(self.batch_seed, case_id, attempt_index, "charge").random()
        return AttemptOutcome(
            success=roll < p,
            probability=round(p, 6),
            roll=round(roll, 6),
            hours_since_original=round(hours_since_original, 2),
            contact_uplift_applied=customer_acted,
        )

    def contact(self, case_id: str, channel: Channel, step_index: int) -> ContactOutcome:
        p = float(self.cfg.contact_response.get(str(channel), 0.0))
        roll = _rng(self.batch_seed, case_id, step_index, f"contact:{channel}").random()
        return ContactOutcome(customer_acted=roll < p, probability=round(p, 6), roll=round(roll, 6))
