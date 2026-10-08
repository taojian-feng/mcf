"""Turn config/pilot.yaml into the App Configuration keys the Directory and the client registry read.

    python -m tower.tools.people config/pilot.yaml   ->  prints {"mcf:roles": ..., "mcf:contacts": ..., ...}

Each retail client has a section under `clients:` with its people and its contract. Corp B is tenant one;
an older file with a top-level `corp_b:` section still works. Blank slots fall back to default_email, so
one address can cover the whole pilot.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from tower.core.models import CLIENT_CATEGORIES, ApprovalCategory, OwningParty
from tower.core.tenants import PROVIDER, TENANT_ONE, Tenant, tenant_one
from tower.services.approval import approver_role, backup_role

AGENTS_WITH_INSIGHT_ACCESS = ["planning_agent", "reporting_agent"]
PILOT_CONTACTS = {"program_lead": "pilot_program_lead", "governance_owner": "pilot_governance_owner",
                  "exception_operator": "exception_operator", "fulfillment_ops": "fulfillment_ops",
                  "ops_team": "ops-team"}
PARTY_CONTACTS = {"ops": "{p}_ops", "escalation": "{p}_escalation", "data": "{p}_data_contact",
                  "inventory": "{p}_inventory", "planner": "{p}_planner", "review": "{p}_review",
                  "billing": "{p}_billing"}
CONTRACT_FIELDS = ("name", "active", "order_prefix", "feed_folder", "api_key_secret", "outbound_url",
                   "outbound_key_secret", "promise_days", "channels", "online_protect_days", "store_lead_days",
                   "return_window_days", "return_ship_by_days")


def _emails(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if str(v).strip()]


def clients(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Client sections by tenant id. Tenant one is always there, from `clients.corp_b` or the old `corp_b:`."""
    out = {tid: dict(sec or {}) for tid, sec in (doc.get("clients") or {}).items()}
    legacy = dict(doc.get(TENANT_ONE) or {})
    if "can_see_insight_data" in doc:
        legacy.setdefault("can_see_insight_data", doc["can_see_insight_data"])
    out[TENANT_ONE] = {**legacy, **out.get(TENANT_ONE, {})}
    return out


def tenant(tid: str, section: dict[str, Any]) -> Tenant:
    base = tenant_one().model_dump() if tid == TENANT_ONE else {"tenant_id": tid, "name": tid.replace("_", " ").title()}
    given = {k: section[k] for k in CONTRACT_FIELDS if section.get(k) not in (None, "")}
    states = section.get("delivery_states")
    if states and states != "us48":
        given["eligible_states"] = sorted({s.upper() for s in states})
    return Tenant.model_validate({**base, **given, "tenant_id": tid})


def build(doc: dict[str, Any]) -> dict[str, Any]:
    default = _emails(doc.get("default_email"))
    roles: dict[str, set[str]] = {}
    contacts: dict[str, str] = {}
    insight: dict[str, set[str]] = {}

    def grant(people: list[str], role: str) -> None:
        for person in people:
            roles.setdefault(person, set()).add(role)

    def people(key: str, section: dict[str, Any], party: OwningParty, tid: str | None) -> set[str]:
        approvers = section.get("approvers") or {}
        backups = section.get("backup_approvers") or {}
        named = set()
        for category in (ApprovalCategory if party == OwningParty.CORP_A else CLIENT_CATEGORIES):
            main = _emails(approvers.get(category.value)) or default
            grant(main, approver_role(category, party, tid))
            backup = _emails(backups.get(category.value))
            grant(backup, backup_role(category, party, tid))
            named |= set(main) | set(backup)
        given = section.get("contacts") or {}
        for slot, name in PARTY_CONTACTS.items():
            address = (_emails(given.get(slot)) or default or [None])[0]
            if address:
                contacts[name.format(p=key)] = address
        return named

    registry: dict[str, Any] = {}
    for tid, section in sorted(clients(doc).items()):
        t = tenant(tid, section)
        registry[tid] = t.model_dump(mode="json", exclude={"tenant_id"})
        approvers = people(tid, section, OwningParty.CORP_B, tid)
        # A client's approvers see the data behind what they approve; nobody sees another client's data.
        insight[tid] = set(AGENTS_WITH_INSIGHT_ACCESS) | set(_emails(section.get("can_see_insight_data"))) | approvers
    people(PROVIDER, doc.get(PROVIDER) or {}, OwningParty.CORP_A, None)

    pilot = doc.get("pilot") or {}
    for slot, key in PILOT_CONTACTS.items():
        address = (_emails(pilot.get(slot)) or default or [None])[0]
        if address:
            contacts[key] = address

    return {"mcf:roles": {p: sorted(rs) for p, rs in sorted(roles.items())},
            "mcf:contacts": dict(sorted(contacts.items())),
            "mcf:pilot_authorization_list": sorted(insight[TENANT_ONE]),
            "mcf:insight_access": {tid: sorted(ps) for tid, ps in sorted(insight.items())},
            "mcf:tenants": registry}


def main(argv: list[str]) -> None:
    import yaml
    with open(argv[0]) as f:
        print(json.dumps(build(yaml.safe_load(f) or {})))


if __name__ == "__main__":
    main(sys.argv[1:])
