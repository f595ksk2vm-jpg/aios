"""External integrations.

A *connector* is a named REST endpoint plus the name of the secret that
authenticates it. Each one becomes a ``<name>_request`` tool. Presets ship for
common systems; operators can register more at runtime without writing code.
The credential is injected by the gateway at call time — the agent only ever
sees the connector's name.
"""
from __future__ import annotations

import asyncio
import json
import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

from ..errors import AiosError, Conflict
from ..models import SystemState
from .base import Tool, ToolContext, schema
from .builtin import check_url

CONNECTORS_KEY = "connectors"

PRESETS = [
    {"name": "github", "base_url": "https://api.github.com", "secret_name": "GITHUB_TOKEN", "auth": "Bearer {secret}",
     "group": "code hosting", "description": "GitHub REST API (repositories, issues, pull requests, actions)."},
    {"name": "gitlab", "base_url": "https://gitlab.com/api/v4", "secret_name": "GITLAB_TOKEN", "auth": "Bearer {secret}",
     "group": "code hosting", "description": "GitLab REST API (projects, merge requests, pipelines)."},
    {"name": "slack", "base_url": "https://slack.com/api", "secret_name": "SLACK_BOT_TOKEN", "auth": "Bearer {secret}",
     "group": "messaging", "description": "Slack Web API (post messages, read channels)."},
    {"name": "hubspot", "base_url": "https://api.hubapi.com", "secret_name": "HUBSPOT_TOKEN", "auth": "Bearer {secret}",
     "group": "crm", "description": "HubSpot CRM API (contacts, companies, deals)."},
]


def _connector_tool(c: dict) -> Tool:
    name, base, secret_name, auth = c["name"], c["base_url"].rstrip("/"), c.get("secret_name"), c.get("auth", "Bearer {secret}")

    async def handler(ctx: ToolContext, a: dict) -> str:
        k = ctx.kernel
        url = base + "/" + a["path"].lstrip("/")
        if urlparse(url).netloc != urlparse(base).netloc:
            raise AiosError("path must stay on the connector's host")
        await check_url(k.settings, url)
        headers = {"Accept": "application/json", "User-Agent": "aios-agent/0.1"}
        if secret_name:
            secret = await k.secrets.get(secret_name, await k.namespaces.chain_ids(ctx.namespace_id))
            if not secret:
                raise AiosError(f"the {name} integration is not configured (secret {secret_name} is not set)")
            headers[c.get("auth_header", "Authorization")] = auth.format(secret=secret)
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                r = await client.request(a["method"].upper(), url, headers=headers, params=a.get("query"),
                                         json=a.get("json") if "json" in a else None)
            except httpx.HTTPError as e:
                raise AiosError(f"{name} request failed: {type(e).__name__}: {e}")
        return f"HTTP {r.status_code}\n{r.text[:15000]}"

    return Tool(
        f"{name}_request", f"{c.get('description', name)} Call the API by method and path relative to {base}. "
        "Authentication is added for you.",
        schema({"method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]}, "path": {"type": "string"},
                "query": {"type": "object"}, "json": {}}, ["method", "path"]),
        handler, f"integration.{name}", lambda a: "read" if a.get("method", "").upper() == "GET" else "external_comm",
        group=c.get("group", "integrations"))


async def email_send(ctx: ToolContext, a: dict) -> str:
    k = ctx.kernel
    url = await k.secrets.get("SMTP_URL", await k.namespaces.chain_ids(ctx.namespace_id))
    if not url:
        raise AiosError("email is not configured (secret SMTP_URL, e.g. smtps://user:password@smtp.example.com:465)")
    u = urlparse(url)
    msg = EmailMessage()
    msg["From"] = a.get("from") or u.username or "aios@localhost"
    msg["To"] = ", ".join(a["to"])
    msg["Subject"] = a["subject"]
    msg.set_content(a["body"])

    def send() -> None:
        cls = smtplib.SMTP_SSL if u.scheme == "smtps" else smtplib.SMTP
        with cls(u.hostname, u.port or (465 if u.scheme == "smtps" else 587), timeout=30) as smtp:
            if u.scheme == "smtp+tls":
                smtp.starttls()
            if u.username:
                smtp.login(u.username, u.password or "")
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(send)
    except (smtplib.SMTPException, OSError) as e:
        raise AiosError(f"sending failed: {type(e).__name__}: {e}")
    return f"email sent to {', '.join(a['to'])}"


async def automation_trigger(ctx: ToolContext, a: dict) -> str:
    """POST a payload to a named webhook (Zapier, n8n, Make, internal services). The URL lives in the secret store."""
    k = ctx.kernel
    url = await k.secrets.get(f"WEBHOOK_{a['hook'].upper()}", await k.namespaces.chain_ids(ctx.namespace_id))
    if not url:
        raise AiosError(f"no webhook named {a['hook']!r} is configured (secret WEBHOOK_{a['hook'].upper()})")
    await check_url(k.settings, url)
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(url, json=a.get("payload") or {})
        except httpx.HTTPError as e:
            raise AiosError(f"webhook call failed: {type(e).__name__}: {e}")
    return f"HTTP {r.status_code}\n{r.text[:4000]}"


class ConnectorService:
    def __init__(self, kernel):
        self.kernel = kernel
        self._custom: list[dict] = []

    async def load(self) -> None:
        reg = self.kernel.tools
        for c in PRESETS:
            reg.register(_connector_tool(c))
        async with self.kernel.db.session() as s:
            row = await s.get(SystemState, CONNECTORS_KEY)
        self._custom = list((row.value or {}).get("items", [])) if row else []
        for c in self._custom:
            reg.register(_connector_tool(c))
        st = {"type": "string"}
        reg.register(Tool("email_send", "Send an email on behalf of the organization.",
                          schema({"to": {"type": "array", "items": st, "minItems": 1}, "subject": st, "body": st, "from": st},
                                 ["to", "subject", "body"]), email_send, "integration.email", "external_comm", "messaging"))
        reg.register(Tool("automation_trigger", "Trigger a configured automation webhook (Zapier, n8n, Make, internal services) by name.",
                          schema({"hook": st, "payload": {"type": "object"}}, ["hook"]),
                          automation_trigger, "integration.automation", "external_comm", "automation"))

    async def add(self, c: dict) -> dict:
        name = c["name"].strip().lower()
        if not name.isidentifier():
            raise Conflict("connector name must be a simple identifier")
        if self.kernel.tools.get(f"{name}_request"):
            raise Conflict(f"a connector named {name!r} already exists")
        if urlparse(c["base_url"]).scheme not in {"http", "https"}:
            raise Conflict("base_url must be http(s)")
        c = {**c, "name": name, "custom": True}
        self._custom.append(c)
        await self._save()
        self.kernel.tools.register(_connector_tool(c))
        return c

    async def remove(self, name: str) -> None:
        if not any(c["name"] == name for c in self._custom):
            raise Conflict("only custom connectors can be removed")
        self._custom = [c for c in self._custom if c["name"] != name]
        await self._save()
        self.kernel.tools.unregister(f"{name}_request")

    async def _save(self) -> None:
        async with self.kernel.db.session() as s:
            row = await s.get(SystemState, CONNECTORS_KEY)
            if row:
                row.value = {"items": json.loads(json.dumps(self._custom))}
            else:
                s.add(SystemState(key=CONNECTORS_KEY, value={"items": self._custom}))

    async def status(self) -> list[dict]:
        """Which integrations exist and whether their credential is present (never the credential itself)."""
        names = {s.name for s in await self.kernel.secrets.list()}
        out = [{"name": c["name"], "base_url": c["base_url"], "group": c.get("group", "integrations"),
                "description": c.get("description", ""), "secret_name": c.get("secret_name"),
                "configured": (not c.get("secret_name")) or c["secret_name"] in names, "custom": bool(c.get("custom")),
                "tool": f"{c['name']}_request"} for c in PRESETS + self._custom]
        out.append({"name": "email", "base_url": "smtp", "group": "messaging", "description": "Outbound email over SMTP.",
                    "secret_name": "SMTP_URL", "configured": "SMTP_URL" in names, "custom": False, "tool": "email_send"})
        hooks = sorted(n[8:].lower() for n in names if n.startswith("WEBHOOK_"))
        out.append({"name": "automation", "base_url": "webhooks", "group": "automation",
                    "description": "Named webhooks for Zapier, n8n, Make and internal services"
                                   + (f" (configured: {', '.join(hooks)})" if hooks else "."),
                    "secret_name": "WEBHOOK_<NAME>", "configured": bool(hooks), "custom": False, "tool": "automation_trigger"})
        return out
