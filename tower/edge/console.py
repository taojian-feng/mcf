"""Approval console: where approvers read and decide what the agents ask for (R8).

  GET  /approvals?t=<link token>        everything this approver may decide, pending first
  GET  /approvals/{id}?t=<link token>   one request: what, why, the agent's numbers, approve / reject
  POST /approvals/{id}                  the decision (form: t, decision=approve|reject, reason)
  GET  /ask?t=<link token>              the operator copilot: ask a question about your clients' work
  POST /ask                             the question (form: t, q); the answer comes back on the page

Approvers arrive from the signed link in their approval email (services/approval_links.py). The link
says who they are; ApprovalService.decide checks their approver role and writes the audit ledger, so
the console adds no permissions of its own.

Run on Azure with `python -m tower.edge.console` (port $PORT, default 8080).
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import parse_qs, quote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tower.core.models import ApprovalItem, ApprovalStatus
from tower.services.approval import ApprovalError, ApprovalService, owner_label
from tower.services.approval_links import ApprovalLinks

log = logging.getLogger("tower.console")

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",            # the sign-in token is in the URL
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
}


QUESTIONS_PER_HOUR = 30   # per person; each question is a few Claude calls


class Console:
    def __init__(self, approvals: ApprovalService, links: ApprovalLinks, copilot=None):
        self.approvals, self.links, self.copilot = approvals, links, copilot
        self._asked: dict[str, deque] = defaultdict(deque)

    def user(self, token: str) -> str | None:
        return self.links.verify(token) if token else None

    def inbox(self, user: str, token: str) -> str:
        items = self.approvals.visible_to(user)
        pending = [i for i in items if i.status == ApprovalStatus.PENDING]
        decided = [i for i in items if i.status != ApprovalStatus.PENDING][:20]
        body = [f"<h1>Approvals</h1><p class=muted>Signed in as {escape(user)}"
                + (f" · <a href='/ask?t={quote(token)}'>Ask the tower</a>" if self.copilot else "") + "</p>"]
        body.append(f"<h2>Waiting for you ({len(pending)})</h2>")
        body.append(_rows(pending, token) if pending else "<p class=muted>Nothing is waiting. You're all caught up.</p>")
        if decided:
            body.append("<h2>Recently decided</h2>" + _rows(decided, token))
        return _page("Approvals", "".join(body))

    def detail(self, user: str, token: str, approval_id: str, notice: str = "", error: str = "") -> str:
        item = self.approvals.get(approval_id)
        back = f"<p><a href='/approvals?t={quote(token)}'>&larr; All approvals</a></p>"
        out = [back, f"<h1>{escape(_title(item))}</h1>", _badge(item.status)]
        if notice:
            out.append(f"<p class=ok>{escape(notice)}</p>")
        if error:
            out.append(f"<p class=err>{escape(error)}</p>")
        if item.brief:
            out.append(_brief(item.brief))
        out.append("<dl>")
        out += [f"<dt>{k}</dt><dd>{v}</dd>" for k, v in [
            ("Why", escape(item.rationale)),
            ("Requested by", escape(item.originating_agent.replace("_", " "))),
            ("Owning party", escape(owner_label(item))),
            ("Requested", _when(item.created_at)),
            ("Reference", f"<code>{escape(item.approval_id)}</code>"),
        ]]
        out.append("</dl><h2>Details</h2>" + _content(item.content))
        if item.status == ApprovalStatus.PENDING:
            if self.approvals.can_decide(user, item):
                out.append(_form(item.approval_id, token))
            else:
                out.append("<p class=muted>You can view this request, but deciding it needs another approver.</p>")
        else:
            reason = f": {escape(item.reason)}" if item.reason else ""
            out.append(f"<p>{item.status.value.capitalize()} by {escape(item.decided_by or '')} "
                       f"on {_when(item.decided_at)}{reason}</p>")
        return _page(_title(item), "".join(out))

    def ask_page(self, user: str, token: str, question: str = "", answer: dict[str, Any] | None = None,
                 error: str = "") -> str:
        out = [f"<p><a href='/approvals?t={quote(token)}'>&larr; Approvals</a></p><h1>Ask the tower</h1>",
               "<p class=muted>Ask about your clients' orders, exceptions, handoffs, approvals, stock and returns. "
               "The copilot only reads; it can't approve or change anything.</p>"]
        if error:
            out.append(f"<p class=err>{escape(error)}</p>")
        if answer:
            out.append(f"<section class=brief><p class=muted>{escape(question)}</p>"
                       f"<div class=answer>{escape(answer['answer'])}</div>")
            if answer.get("sources"):
                used = sorted({s["tool"].replace("_", " ") for s in answer["sources"]})
                out.append(f"<p class=muted>Looked at: {escape(', '.join(used))}. Written by Claude; check the "
                           "records before acting on it.</p>")
            out.append("</section>")
        out.append(f"<form method=post action='/ask'><input type=hidden name=t value='{escape(token, quote=True)}'>"
                   "<label for=q>Your question</label>"
                   f"<textarea id=q name=q rows=3 maxlength=1000 placeholder='Why is order 1001 late?'>"
                   f"{escape(question) if not answer else ''}</textarea>"
                   "<div class=actions><button class=primary>Ask</button></div></form>")
        return _page("Ask the tower", "".join(out))

    def ask(self, user: str, question: str) -> dict[str, Any]:
        q = self._asked[user]
        now = time.monotonic()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= QUESTIONS_PER_HOUR:
            raise ApprovalError(f"That's {QUESTIONS_PER_HOUR} questions this hour. Try again a little later.")
        q.append(now)
        return self.copilot.ask(user, question)

    def decide(self, user: str, approval_id: str, decision: str, reason: str) -> str:
        if decision not in ("approve", "reject"):
            raise ApprovalError("choose approve or reject")
        item = self.approvals.decide(user, approval_id, approve=decision == "approve", reason=reason.strip())
        return f"{item.status.value.capitalize()}. The agent has been told and the decision is in the audit log."


def create_app(console: Console) -> Starlette:
    def html(text: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(text, status, headers=HEADERS)

    def signed_out() -> HTMLResponse:
        return html(_page("Link expired", "<h1>This link has expired or isn't valid</h1>"
                          "<p>Open the newest approval email and use the link in it.</p>"), 401)

    async def inbox(request: Request) -> Response:
        token = request.query_params.get("t", "")
        user = console.user(token)
        if not user:
            return signed_out()
        return html(await run_in_threadpool(console.inbox, user, token))

    async def detail(request: Request) -> Response:
        token, aid = request.query_params.get("t", ""), request.path_params["approval_id"]
        user = console.user(token)
        if not user:
            return signed_out()
        notice = {"approved": "Approved. The agent has been told and the decision is in the audit log.",
                  "rejected": "Rejected. The agent has been told and the decision is in the audit log."}
        try:
            return html(await run_in_threadpool(console.detail, user, token, aid,
                                                notice.get(request.query_params.get("done", ""), "")))
        except ApprovalError:
            return html(_page("Not found", "<h1>That approval doesn't exist</h1>"), 404)

    async def decide(request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
        token, aid = form.get("t", ""), request.path_params["approval_id"]
        user = console.user(token)
        if not user:
            return signed_out()
        try:
            await run_in_threadpool(console.decide, user, aid, form.get("decision", ""), form.get("reason", ""))
        except PermissionError:
            return html(await run_in_threadpool(console.detail, user, token, aid, "",
                                                "You don't have the approver role for this request."), 403)
        except ApprovalError as e:
            return html(await run_in_threadpool(console.detail, user, token, aid, "", str(e)), 409)
        done = "approved" if form.get("decision") == "approve" else "rejected"
        return RedirectResponse(f"/approvals/{quote(aid)}?t={quote(token)}&done={done}", 303, headers=HEADERS)

    async def ask(request: Request) -> Response:
        if request.method == "GET":
            token = request.query_params.get("t", "")
        else:
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
            token = form.get("t", "")
        user = console.user(token)
        if not user:
            return signed_out()
        if console.copilot is None:
            return html(_page("Not available", "<h1>The copilot isn't switched on here</h1>"), 404)
        if request.method == "GET":
            return html(console.ask_page(user, token))
        question = form.get("q", "").strip()
        if not question:
            return html(console.ask_page(user, token, error="Type a question first."), 400)
        try:
            answer = await run_in_threadpool(console.ask, user, question)
        except ApprovalError as e:
            return html(console.ask_page(user, token, question, error=str(e)), 429)
        return html(console.ask_page(user, token, question, answer))

    async def home(request: Request) -> Response:
        return html(_page("Approvals", "<h1>Control Tower approvals</h1>"
                          "<p>Open the link in your approval email to see what's waiting for you.</p>"))

    return Starlette(routes=[
        Route("/", home),
        Route("/healthz", lambda r: JSONResponse({"ok": True, "service": "console",
                                                  "time": datetime.now(timezone.utc).isoformat()})),
        Route("/approvals", inbox),
        Route("/approvals/{approval_id}", detail, methods=["GET"]),
        Route("/approvals/{approval_id}", decide, methods=["POST"]),
        Route("/ask", ask, methods=["GET", "POST"]),
    ])


# ---------------------------------------------------------------- rendering

def _title(item: ApprovalItem) -> str:
    return f"{item.category.value.replace('_', ' ').capitalize()} for {item.subject_id}"


def _when(t: datetime | None) -> str:
    return f"{t:%Y-%m-%d %H:%M} UTC" if t else ""


def _badge(status: ApprovalStatus) -> str:
    return f"<span class='badge {status.value}'>{status.value}</span>"


def _rows(items: list[ApprovalItem], token: str) -> str:
    rows = "".join(
        f"<tr><td><a href='/approvals/{quote(i.approval_id)}?t={quote(token)}'>{escape(_title(i))}</a></td>"
        f"<td>{escape(i.originating_agent.replace('_', ' '))}</td><td>{_when(i.created_at)}</td>"
        f"<td>{_badge(i.status)}</td></tr>" for i in items)
    return f"<table><tr><th>Request</th><th>From</th><th>Requested</th><th>Status</th></tr>{rows}</table>"


def _content(content: dict[str, Any]) -> str:
    if not content:
        return "<p class=muted>No further details.</p>"
    rows = []
    for k, v in content.items():
        if isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            cell = escape(", ".join(map(str, v)))
        elif isinstance(v, (dict, list)):
            cell = f"<pre>{escape(json.dumps(v, indent=2, default=str))}</pre>"
        else:
            cell = escape(str(v))
        rows.append(f"<tr><th>{escape(k.replace('_', ' '))}</th><td>{cell}</td></tr>")
    return f"<table class=kv>{''.join(rows)}</table>"


SUGGESTION = {"approve": "Suggests approving", "reject": "Suggests rejecting", "look_closer": "Suggests a closer look"}


def _brief(b: dict[str, Any]) -> str:
    """The plain-language brief. It is advice: the approver decides, and the details below are the record."""
    out = [f"<section class=brief><p>{escape(b.get('summary', ''))}</p><dl>",
           f"<dt>If you approve</dt><dd>{escape(b.get('if_approved', ''))}</dd>",
           f"<dt>If you reject</dt><dd>{escape(b.get('if_rejected', ''))}</dd>"]
    for k in b.get("key_numbers") or []:
        out.append(f"<dt>{escape(str(k.get('label', '')))}</dt><dd>{escape(str(k.get('value', '')))}</dd>")
    out.append("</dl>")
    if b.get("check_first"):
        out.append("<p class=muted>Worth checking first:</p><ul>"
                   + "".join(f"<li>{escape(c)}</li>" for c in b["check_first"]) + "</ul>")
    if b.get("suggestion") in SUGGESTION:
        out.append(f"<p><strong>{SUGGESTION[b['suggestion']]}</strong>: {escape(b.get('suggestion_reason', ''))}</p>")
    who = "Written by Claude from this request and the tower's records" if b.get("source") == "claude" \
        else "Written from this request's numbers"
    out.append(f"<p class=muted>{who}. It is advice; you decide.</p></section>")
    return "".join(out)


def _form(approval_id: str, token: str) -> str:
    return (f"<form method=post action='/approvals/{quote(approval_id)}'>"
            f"<input type=hidden name=t value='{escape(token, quote=True)}'>"
            "<label for=reason>Reason (required to reject)</label>"
            "<textarea id=reason name=reason rows=3></textarea>"
            "<div class=actions><button name=decision value=approve class=primary>Approve</button>"
            "<button name=decision value=reject>Reject</button></div></form>")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>{escape(title)} · Control Tower</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--card:#fff;--accent:#2563eb;--ok:#15803d;--err:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--card:#292524;--accent:#60a5fa;--ok:#4ade80;--err:#f87171}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}header{{border-bottom:1px solid var(--line);padding:12px 16px;font-weight:600}}
h1{{font-size:22px;margin:8px 0}}h2{{font-size:16px;margin:28px 0 8px}}a{{color:var(--accent)}}
.muted{{color:var(--muted)}}.ok{{color:var(--ok)}}.err{{color:var(--err)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
.brief{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:4px 14px;margin:16px 0}}
.answer{{white-space:pre-wrap;margin:8px 0}}
.kv th{{width:32%;color:var(--muted);font-weight:500}}pre{{margin:0;white-space:pre-wrap;font-size:13px}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px}}dt{{color:var(--muted)}}dd{{margin:0}}
.badge{{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line)}}
.badge.pending{{color:var(--accent)}}.badge.approved{{color:var(--ok)}}.badge.rejected{{color:var(--err)}}
form{{margin-top:24px}}label{{display:block;margin-bottom:4px;color:var(--muted)}}
textarea{{width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}}
.actions{{display:flex;gap:8px;margin-top:12px}}button{{font:inherit;padding:8px 18px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}}
button.primary{{background:var(--accent);border-color:var(--accent);color:#fff}}
</style></head><body><header>Logistics Control Tower</header><main>{body}</main></body></html>"""


def main() -> None:
    import os

    import uvicorn

    from tower.core.config import PilotConfig
    from tower.runtime import approval_links, from_env
    from tower.services.audit import AuditService

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ports = from_env()
    links = approval_links(ports)
    if links is None:
        raise SystemExit("the console needs MCF_CONSOLE_URL and a secret store")
    load = getattr(ports.directory, "pilot_config", None)
    config = load() if load else PilotConfig()
    from tower.agents.copilot import Copilot
    from tower.edge.connectors import load_catalogs

    audit = AuditService(ports.ledger, ports.blobs, ports.notifier, ports.clock)
    approvals = ApprovalService(ports.store, ports.bus, ports.directory, ports.notifier, audit, ports.clock, config,
                                links=links)
    copilot = None
    if ports.llm is not None:
        from types import SimpleNamespace
        load_catalogs(SimpleNamespace(config=config, ports=ports))   # which client owns which SKU
        copilot = Copilot(ports.store, ports.ledger, ports.directory, audit, ports.clock, config, ports.llm)
    uvicorn.run(create_app(Console(approvals, links, copilot)), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                log_level="warning")


if __name__ == "__main__":
    main()
