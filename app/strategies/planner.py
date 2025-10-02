# Minimal planner stub for /plan
from dataclasses import dataclass
from typing import Dict, List

@dataclass
class Action:
    action_id: str
    bot: str
    route_human: str
    amount_in_text: str
    reason: str = ""
    limits_text: str = ""
    priority: int = 3

def build_plan_snapshot() -> Dict[str, List[Action]]:
    # Return an empty plan until real logic is in place
    return {
        "tecbot_eth": [],
        "tecbot_usdc": [],
        "tecbot_sdai": [],
        "tecbot_tec": [],
    }
