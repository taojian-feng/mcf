"""Email approvers a fresh link to the approval console, without waiting for an approval request.

The console is reached only through signed links, and those normally arrive in approval emails. When the
agents have not asked for anything in a while there is no email to click. This sends each approver (people
with an approver role in the people file) one email with a link to their approvals and to "Ask the tower".
The link is signed with the same Key Vault key as approval emails and expires the same way.

    python -m tower.tools.console_link --people people.json --console-url https://... \\
        --vault https://<vault>.vault.azure.net --acs "<connection string>" --sender DoNotReply@... [--to someone@...]

The connection string is read from the MCF_ACS_CONNECTION environment variable when --acs is not given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from tower.services.approval_links import DEFAULT_TTL, LINK_KEY_SECRET, ApprovalLinks


class _Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def approvers(people: dict) -> list[str]:
    """Everyone with an approver or backup approver role, from `python -m tower.tools.people` output."""
    return sorted(p for p, roles in (people.get("mcf:roles") or {}).items()
                  if "@" in p and any(r.startswith(("approver:", "approver_backup:")) for r in roles))


def message(links: ApprovalLinks, user: str) -> tuple[str, str]:
    inbox = links.inbox_url(user)
    ask = inbox.replace("/approvals?", "/ask?", 1)
    body = "\n".join([
        "Here is your link to the Control Tower approval console.", "",
        "Approvals waiting for you:", inbox, "",
        "Ask the tower (questions about orders, stock, returns and more):", ask, "",
        f"The links sign you in as you and work for {DEFAULT_TTL.days} days. Don't forward this email."])
    return "[Control Tower info] Your approval console link", body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--people", required=True, help="JSON from python -m tower.tools.people")
    ap.add_argument("--console-url", required=True)
    ap.add_argument("--vault", required=True, help="Key Vault URL holding approval-link-key")
    ap.add_argument("--acs", default=os.environ.get("MCF_ACS_CONNECTION"), help="Communication Services connection string")
    ap.add_argument("--sender", required=True)
    ap.add_argument("--to", default="", help="one approver; blank sends to every approver")
    args = ap.parse_args(argv)

    with open(args.people) as f:
        people = approvers(json.load(f))
    targets = [args.to] if args.to else people
    if not targets or any(t not in people for t in targets):
        print(f"Only people with an approver role can get a console link. Approvers: {', '.join(people) or 'none'}")
        return 1

    from azure.communication.email import EmailClient
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    key = SecretClient(args.vault, DefaultAzureCredential()).get_secret(LINK_KEY_SECRET).value.encode()
    links = ApprovalLinks(args.console_url, key, _Clock())
    mail = EmailClient.from_connection_string(args.acs)
    failed = 0
    for user in targets:
        subject, body = message(links, user)
        result = mail.begin_send({"senderAddress": args.sender, "recipients": {"to": [{"address": user}]},
                                  "content": {"subject": subject, "plainText": body}}).result(timeout=120)
        print(f"- {user}: {result['status']}")
        failed += result["status"] != "Succeeded"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
