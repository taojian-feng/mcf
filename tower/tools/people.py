"""Turn config/pilot.yaml into the App Configuration keys the Directory reads.

    python -m tower.tools.people config/pilot.yaml   ->  prints {"mcf:roles": ..., "mcf:contacts": ..., ...}

Blank slots fall back to default_email, so one address can cover the whole pilot.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from tower.core.models import ApprovalCategory, OwningParty
from tower.services.approval import approver_role, backup_role

AGENTS_WITH_INSIGHT_ACCESS = ["planning_agent", "reporting_agent"]
PILOT_CONTACTS = {"program_lead": "pilot_program_lead", "governance_owner": "pilot_governance_owner",
                  "exception_operator": "exception_operator", "fulfillment_ops": "fulfillment_ops",
                  "ops_team": "ops-team"}
PARTY_CONTACTS = {"ops": "{p}_ops", "escalation": "{p}_escalation", "data": "{p}_data_contact",
                  "inventory": "{p}_inventory", "planner": "{p}_planner", "review": "{p}_review"}


def _emails(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if str(v).strip()]


def build(doc: dict[str, Any]) -> dict[str, Any]:
    default = _emails(doc.get("default_email"))
    roles: dict[str, set[str]] = {}
    contacts: dict[str, str] = {}

    def grant(people: list[str], role: str) -> None:
        for person in people:
            roles.setdefault(person, set()).add(role)

    for party in OwningParty:
        section = doc.get(party.value) or {}
        approvers = section.get("approvers") or {}
        backups = section.get("backup_approvers") or {}
        for category in ApprovalCategory:
            grant(_emails(approvers.get(category.value)) or default, approver_role(category, party))
            grant(_emails(backups.get(category.value)), backup_role(category, party))
        given = section.get("contacts") or {}
        for slot, key in PARTY_CONTACTS.items():
            address = (_emails(given.get(slot)) or default or [None])[0]
            if address:
                contacts[key.format(p=party.value)] = address

    pilot = doc.get("pilot") or {}
    for slot, key in PILOT_CONTACTS.items():
        address = (_emails(pilot.get(slot)) or default or [None])[0]
        if address:
            contacts[key] = address

    insight = set(AGENTS_WITH_INSIGHT_ACCESS) | set(_emails(doc.get("can_see_insight_data")))
    insight |= {p for p, rs in roles.items() if any(r.endswith(f":{OwningParty.CORP_B.value}") for r in rs)}
    return {"mcf:roles": {p: sorted(rs) for p, rs in sorted(roles.items())},
            "mcf:contacts": dict(sorted(contacts.items())),
            "mcf:pilot_authorization_list": sorted(insight)}


def main(argv: list[str]) -> None:
    import yaml
    with open(argv[0]) as f:
        print(json.dumps(build(yaml.safe_load(f) or {})))


if __name__ == "__main__":
    main(sys.argv[1:])
