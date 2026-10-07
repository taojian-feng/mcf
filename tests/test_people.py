from tower.core.models import ApprovalCategory, OwningParty
from tower.services.approval import approver_role
from tower.tools.people import build


def test_one_default_email_covers_every_approver_and_contact():
    out = build({"default_email": "me@x.com"})
    roles = set(out["mcf:roles"]["me@x.com"])
    assert {approver_role(c, p) for c in ApprovalCategory for p in OwningParty} <= roles
    assert out["mcf:contacts"]["exception_operator"] == "me@x.com"
    assert out["mcf:contacts"]["corp_b_inventory"] == "me@x.com"
    assert out["mcf:contacts"]["ops-team"] == "me@x.com"
    assert "me@x.com" in out["mcf:pilot_authorization_list"]
    assert "reporting_agent" in out["mcf:pilot_authorization_list"]


def test_named_slots_override_the_default():
    out = build({"default_email": "me@x.com",
                 "corp_b": {"approvers": {"disposition": ["ann@b.com", "raj@b.com"]},
                            "contacts": {"inventory": "inv@b.com"}},
                 "pilot": {"program_lead": "lead@b.com"}})
    disp = approver_role(ApprovalCategory.DISPOSITION, OwningParty.CORP_B)
    assert disp in out["mcf:roles"]["ann@b.com"] and disp in out["mcf:roles"]["raj@b.com"]
    assert disp not in out["mcf:roles"]["me@x.com"]
    assert out["mcf:contacts"]["corp_b_inventory"] == "inv@b.com"
    assert out["mcf:contacts"]["pilot_program_lead"] == "lead@b.com"


def test_blank_file_produces_no_people():
    out = build({})
    assert out["mcf:roles"] == {} and out["mcf:contacts"] == {}
    assert out["mcf:pilot_authorization_list"] == ["planning_agent", "reporting_agent"]
