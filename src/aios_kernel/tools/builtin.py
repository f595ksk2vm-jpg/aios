"""Built-in kernel tools: files, code execution, git, web, SQL, memory."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import re
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from ..errors import AiosError
from ..sandbox import LANGUAGES
from .base import Tool, ToolContext, schema

PROTECTED_DIRS = {".git", ".aios_run"}


def resolve_path(workspace: Path, rel: str, *, for_write: bool = False) -> Path:
    """Map a workspace-relative path to a real path, refusing anything that escapes the workspace."""
    root = workspace.resolve()
    target = (root / rel.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise AiosError(f"path {rel!r} is outside the workspace")
    if for_write and PROTECTED_DIRS & set(target.relative_to(root).parts):
        raise AiosError(f"path {rel!r} is inside a protected directory")
    return target


# --- filesystem -----------------------------------------------------------------------

async def fs_list(ctx: ToolContext, a: dict) -> str:
    base = resolve_path(ctx.workspace, a.get("path", "."))
    if not base.exists():
        raise AiosError(f"{a.get('path', '.')} does not exist")
    if base.is_file():
        return f"{base.relative_to(ctx.workspace.resolve())} ({base.stat().st_size} bytes)"
    lines, root = [], ctx.workspace.resolve()
    walker = base.rglob("*") if a.get("recursive") else base.iterdir()
    for p in sorted(walker):
        rel = p.relative_to(root)
        if PROTECTED_DIRS & set(rel.parts):
            continue
        lines.append(f"{rel}/" if p.is_dir() else f"{rel} ({p.stat().st_size} bytes)")
        if len(lines) >= 500:
            lines.append("... (listing truncated at 500 entries)")
            break
    return "\n".join(lines) or "(empty directory)"


async def fs_read(ctx: ToolContext, a: dict) -> str:
    p = resolve_path(ctx.workspace, a["path"])
    if not p.is_file():
        raise AiosError(f"{a['path']} is not a file")
    data = p.read_bytes()
    if b"\x00" in data[:4096]:
        raise AiosError(f"{a['path']} is a binary file ({len(data)} bytes)")
    text = data.decode(errors="replace")
    start = max(0, int(a.get("offset", 0)))
    return text[start:start + int(a.get("max_chars", 20000))]


async def fs_write(ctx: ToolContext, a: dict) -> str:
    p = resolve_path(ctx.workspace, a["path"], for_write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if a.get("append"):
        with p.open("a") as f:
            f.write(a["content"])
    else:
        p.write_text(a["content"])
    return f"{'updated' if existed else 'created'} {a['path']} ({p.stat().st_size} bytes)"


async def fs_delete(ctx: ToolContext, a: dict) -> str:
    p = resolve_path(ctx.workspace, a["path"], for_write=True)
    if p == ctx.workspace.resolve():
        raise AiosError("refusing to delete the workspace root")
    if not p.exists():
        raise AiosError(f"{a['path']} does not exist")
    if p.is_dir():
        import shutil
        shutil.rmtree(p)
    else:
        p.unlink()
    return f"deleted {a['path']}"


# --- code execution -------------------------------------------------------------------

async def code_execute(ctx: ToolContext, a: dict) -> str:
    k = ctx.kernel
    timeout = min(int(a.get("timeout_s") or k.settings.sandbox_timeout_s), 600)
    res = await k.sandbox.run(workdir=ctx.workspace, language=a["language"], code=a["code"], timeout_s=timeout,
                              allow_network=bool(a.get("network")))
    await k.budget.record(namespace_id=ctx.namespace_id, agent_id=ctx.agent.id, task_id=ctx.task_id, run_id=ctx.run_id,
                          kind="compute", provider=res.driver, model=a["language"], latency_ms=res.duration_ms)
    return res.render()


# --- git ------------------------------------------------------------------------------

async def git(workspace: Path, *args: str, check: bool = True, _retry: int = 0) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "user.name=AI-OS", "-c", "user.email=aios@localhost", "-c", "commit.gpgsign=false",
        "-c", "init.defaultBranch=main", *args,
        cwd=str(workspace), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0 and b"index.lock" in err and _retry < 5:
        # Another process is touching this repository's index; wait our turn.
        await asyncio.sleep(0.2 * (_retry + 1))
        return await git(workspace, *args, check=check, _retry=_retry + 1)
    if check and proc.returncode != 0:
        raise AiosError(f"git {args[0]} failed: {(err or out).decode(errors='replace').strip()[:800]}")
    return out.decode(errors="replace")


async def ensure_repo(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / ".git").exists():
        await git(workspace, "init", "-q")
        (workspace / ".gitignore").write_text(".aios_run/\n__pycache__/\n*.pyc\nnode_modules/\n")


async def commit_all(workspace: Path, message: str, author: str = "AI-OS", paths: list[str] | None = None) -> dict | None:
    """Commit changes in the workspace — everything, or only ``paths``.

    Returns {"commit", "files"} or None if there was nothing to commit.
    Limiting to paths lets tasks that share a workspace each commit their own
    files, so history attributes work to the agent that did it.
    """
    await ensure_repo(workspace)
    if paths is None:
        await git(workspace, "add", "-A")
    else:
        root = workspace.resolve()
        for rel in paths:
            try:
                target = resolve_path(workspace, rel, for_write=True)
            except AiosError:
                continue
            spec = str(target.relative_to(root))
            if target.exists():
                await git(workspace, "add", "-A", "--", spec, check=False)
            else:
                await git(workspace, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", spec, check=False)
    if not (await git(workspace, "diff", "--cached", "--name-only")).strip():
        return None
    await git(workspace, "commit", "-q", "-m", message, f"--author={author} <aios@localhost>")
    sha = (await git(workspace, "rev-parse", "HEAD")).strip()
    files = [line.split("\t") for line in (await git(workspace, "show", "--name-status", "--format=", sha)).splitlines() if line.strip()]
    return {"commit": sha, "files": [{"status": f[0], "path": f[-1]} for f in files]}


async def git_status(ctx: ToolContext, a: dict) -> str:
    await ensure_repo(ctx.workspace)
    return (await git(ctx.workspace, "status", "--short", "--branch")) or "(clean)"


async def git_commit(ctx: ToolContext, a: dict) -> str:
    res = await commit_all(ctx.workspace, a["message"], author=ctx.agent.name)
    if not res:
        return "nothing to commit"
    return f"committed {res['commit'][:10]}: " + ", ".join(f["path"] for f in res["files"][:30])


async def git_log(ctx: ToolContext, a: dict) -> str:
    await ensure_repo(ctx.workspace)
    return (await git(ctx.workspace, "log", f"-{min(int(a.get('limit', 15)), 100)}", "--date=short",
                      "--format=%h %ad %an: %s", check=False)) or "(no commits yet)"


async def git_diff(ctx: ToolContext, a: dict) -> str:
    await ensure_repo(ctx.workspace)
    args = ["diff", a["ref"]] if a.get("ref") else ["diff", "HEAD"]
    return (await git(ctx.workspace, *args, check=False)) or "(no differences)"


# --- web ------------------------------------------------------------------------------

_BLOCKED_NETS = [ipaddress.ip_network(n) for n in ("169.254.0.0/16", "fe80::/10", "100.64.0.0/10")]


async def check_url(settings, url: str) -> None:
    """Egress policy for agent-initiated requests: scheme, host allowlist and no private or metadata addresses."""
    u = urlparse(url)
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise AiosError("only http(s) URLs with a hostname are allowed")
    host = u.hostname.lower()
    if settings.http_allowed_hosts and not any(host == h or host.endswith("." + h) for h in settings.http_allowed_hosts):
        raise AiosError(f"host {host} is not on the egress allowlist")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
            return  # resolution happens at the egress proxy, which cannot reach this host's private networks
        raise AiosError(f"cannot resolve host {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _BLOCKED_NETS):
            raise AiosError("requests to link-local / metadata addresses are never allowed")
        if (ip.is_private or ip.is_loopback or ip.is_reserved) and not settings.http_allow_private:
            raise AiosError(f"host {host} resolves to a private address; set AIOS_HTTP_ALLOW_PRIVATE to permit this")


def html_to_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", body)
    body = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)\s*/?>", "\n", body)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    return re.sub(r"\n\s*\n+", "\n\n", body).strip()


async def _request(settings, method: str, url: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, headers={"User-Agent": "aios-agent/0.1"}) as client:
        for _ in range(6):
            await check_url(settings, url)  # every hop is re-validated, so a redirect cannot smuggle a private address
            r = await client.request(method, url, **kwargs)
            if r.status_code in (301, 302, 303, 307, 308) and "location" in r.headers:
                url = urljoin(url, r.headers["location"])
                if r.status_code == 303:
                    method, kwargs = "GET", {}
                continue
            return r
    raise AiosError("too many redirects")


async def web_fetch(ctx: ToolContext, a: dict) -> str:
    try:
        r = await _request(ctx.kernel.settings, "GET", a["url"])
    except httpx.HTTPError as e:
        raise AiosError(f"request failed: {type(e).__name__}: {e}")
    ctype = r.headers.get("content-type", "")
    text = html_to_text(r.text) if "html" in ctype else r.text
    return f"HTTP {r.status_code} {ctype}\n\n{text[: int(a.get('max_chars', 15000))]}"


async def web_search(ctx: ToolContext, a: dict) -> str:
    base = ctx.kernel.settings.search_url
    if not base:
        raise AiosError("web search is not configured on this deployment (AIOS_SEARCH_URL). Use web_fetch with known URLs instead.")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(base, params={"q": a["query"], "format": "json"})
    r.raise_for_status()
    results = r.json().get("results", [])[: int(a.get("max_results", 8))]
    return "\n\n".join(f"{i + 1}. {x.get('title', '')}\n{x.get('url', '')}\n{x.get('content', '')}" for i, x in enumerate(results)) or "(no results)"


async def http_request(ctx: ToolContext, a: dict) -> str:
    kwargs: dict = {"headers": a.get("headers") or {}}
    if "json" in a:
        kwargs["json"] = a["json"]
    elif "body" in a:
        kwargs["content"] = a["body"]
    try:
        r = await _request(ctx.kernel.settings, a["method"].upper(), a["url"], **kwargs)
    except httpx.HTTPError as e:
        raise AiosError(f"request failed: {type(e).__name__}: {e}")
    return f"HTTP {r.status_code}\n{r.text[:15000]}"


# --- SQL (workspace-local databases) -------------------------------------------------------

def _is_read_sql(sql: str) -> bool:
    return bool(re.match(r"(?is)^\s*(select|with|pragma\s+table_info|explain)\b", sql)) and ";" not in sql.strip().rstrip(";")


async def sql_query(ctx: ToolContext, a: dict) -> str:
    path = resolve_path(ctx.workspace, a["database"], for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)

    def run() -> str:
        con = sqlite3.connect(path, timeout=10)
        try:
            if _is_read_sql(a["sql"]):
                cur = con.execute(a["sql"], a.get("params") or [])
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(200)
                lines = [" | ".join(cols)] + [" | ".join(str(v) for v in row) for row in rows]
                return "\n".join(lines) + ("\n... (first 200 rows)" if len(rows) == 200 else "")
            cur = con.executescript(a["sql"]) if not a.get("params") else con.execute(a["sql"], a["params"])
            con.commit()
            return f"ok ({con.total_changes} rows changed)"
        except sqlite3.Error as e:
            raise AiosError(f"SQL error: {e}")
        finally:
            con.close()

    return await asyncio.to_thread(run)


# --- memory ---------------------------------------------------------------------------

async def memory_search(ctx: ToolContext, a: dict) -> str:
    hits = await ctx.kernel.memory.search(a["query"], namespace_id=ctx.namespace_id, agent_id=ctx.agent.id, k=int(a.get("k", 6)))
    if not hits:
        return "(no relevant memories)"
    return "\n\n".join(f"[{h['score']}] {h['title'] or h['kind']} ({h['scope']}, {h['created_at'][:10]})\n{h['content']}" for h in hits)


async def memory_store(ctx: ToolContext, a: dict) -> str:
    scope = a.get("scope", "shared")
    items = await ctx.kernel.memory.add(namespace_id=ctx.namespace_id, content=a["content"], title=a.get("title", ""),
                                        kind=a.get("kind", "note"), scope="agent" if scope == "private" else "namespace",
                                        agent_id=ctx.agent.id, meta={"task_id": ctx.task_id})
    return f"stored {len(items)} memory item(s) ({'private to you' if scope == 'private' else 'shared with your organization'})"


def register(registry) -> None:
    s = {"type": "string"}
    registry.register(Tool("fs_list", "List files in your workspace. Paths are relative to the workspace root.",
                           schema({"path": s, "recursive": {"type": "boolean"}}), fs_list, "fs.read", "read", "files"))
    registry.register(Tool("fs_read", "Read a text file from your workspace.",
                           schema({"path": s, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ["path"]),
                           fs_read, "fs.read", "read", "files"))
    registry.register(Tool("fs_write", "Create or overwrite a file in your workspace (parent directories are created). "
                           "Set append=true to add to the end instead.",
                           schema({"path": s, "content": s, "append": {"type": "boolean"}}, ["path", "content"]),
                           fs_write, "fs.write", "write_internal", "files"))
    registry.register(Tool("fs_delete", "Delete a file or directory from your workspace.",
                           schema({"path": s}, ["path"]), fs_delete, "fs.write", "write_internal", "files"))
    registry.register(Tool(
        "code_execute",
        "Run code in a sandbox whose working directory is your workspace, and get exit code, stdout and stderr. "
        "Use it to run programs, tests and builds you have written to the workspace. The sandbox has no network "
        "access unless network=true (which may require human approval).",
        schema({"language": {"type": "string", "enum": sorted(LANGUAGES)}, "code": s,
                "timeout_s": {"type": "integer"}, "network": {"type": "boolean"}}, ["language", "code"]),
        code_execute, "exec.code", lambda a: "external_comm" if a.get("network") else "code_exec", "execution", timeout_s=660))
    registry.register(Tool("git_status", "Show the git status of your workspace repository.", schema({}), git_status, "fs.read", "read", "git"))
    registry.register(Tool("git_commit", "Stage and commit all changes in your workspace repository.",
                           schema({"message": s}, ["message"]), git_commit, "fs.write", "write_internal", "git"))
    registry.register(Tool("git_log", "Show recent commits of your workspace repository.",
                           schema({"limit": {"type": "integer"}}), git_log, "fs.read", "read", "git"))
    registry.register(Tool("git_diff", "Show uncommitted changes, or the diff against a ref.",
                           schema({"ref": s}), git_diff, "fs.read", "read", "git"))
    registry.register(Tool("web_fetch", "Fetch a web page or API resource with GET and return its text.",
                           schema({"url": s, "max_chars": {"type": "integer"}}, ["url"]), web_fetch, "net.read", "read", "web"))
    registry.register(Tool("web_search", "Search the web and return result titles, URLs and snippets.",
                           schema({"query": s, "max_results": {"type": "integer"}}, ["query"]), web_search, "net.read", "read", "web"))
    registry.register(Tool(
        "http_request", "Make an HTTP request to an external API. Non-GET methods change state in the outside world.",
        schema({"method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]}, "url": s,
                "headers": {"type": "object", "additionalProperties": {"type": "string"}}, "json": {}, "body": s}, ["method", "url"]),
        http_request, "net.write", lambda a: "read" if a.get("method", "").upper() in {"GET", "HEAD"} else "external_comm", "web"))
    registry.register(Tool(
        "sql_query", "Run SQL against a SQLite database file in your workspace (created if missing).",
        schema({"database": s, "sql": s, "params": {"type": "array"}}, ["database", "sql"]),
        sql_query, "db.query", lambda a: "read" if _is_read_sql(a.get("sql", "")) else "write_internal", "data"))
    registry.register(Tool("memory_search", "Search your own memory and the knowledge shared with your organization.",
                           schema({"query": s, "k": {"type": "integer"}}, ["query"]), memory_search, "memory.read", "read", "memory"))
    registry.register(Tool(
        "memory_store", "Save something worth remembering beyond this task: a decision, a fact, a lesson. "
        "scope 'shared' makes it visible to your organization, 'private' keeps it to you.",
        schema({"title": s, "content": s, "scope": {"type": "string", "enum": ["shared", "private"]},
                "kind": {"type": "string", "enum": ["note", "fact", "decision", "lesson"]}}, ["content"]),
        memory_store, "memory.write", "write_internal", "memory"))
