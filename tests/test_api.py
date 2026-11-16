"""The control plane over HTTP: setup, RBAC, the full flow, backup, and the layer boundaries."""
import ast
import tarfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from aios_api.app import create_app
from aios_kernel.config import Settings
from aios_kernel.llm.scripted import ScriptedProvider
from aios_kernel.llm.types import ChatResult
from aios_kernel.tasks.queue import Worker

from conftest import call, called, fresh_database_url, say, task_title, tool_names, wait_for

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest_asyncio.fixture
async def api(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data", embedded_worker=False, enable_scripted_provider=True,
                              database_url=await fresh_database_url()))
    k = app.state.kernel
    async with app.router.lifespan_context(app):
        for p in await k.models.providers():
            if p.kind != "scripted":
                await k.models.upsert_provider(name=p.name, kind=p.kind, location=p.location, base_url=p.base_url, enabled=False)
        k.worker = Worker(k, location="local", concurrency=4, poll_s=0.05)
        k.worker.start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://aios") as client:
            r = await client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "owner-password", "name": "Owner"})
            assert r.status_code == 200, r.text
            client.headers["authorization"] = f"Bearer {r.json()['token']}"
            yield client, k
    ScriptedProvider.responder = staticmethod(lambda *a: ChatResult(text="(no responder)"))


async def test_auth_setup_login_and_rbac(api):
    client, k = api
    anon = httpx.AsyncClient(transport=client._transport, base_url="http://aios")
    assert (await anon.get("/api/v1/system/status")).status_code == 401
    assert (await anon.post("/api/v1/auth/setup", json={"email": "x@y.z", "password": "another-pass"})).status_code == 409  # one-time only
    assert (await anon.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "wrong"})).status_code == 401

    assert (await client.post("/api/v1/users", json={"email": "viewer@example.com", "password": "viewer-password", "role": "viewer"})).status_code == 200
    tok = (await anon.post("/api/v1/auth/login", json={"email": "viewer@example.com", "password": "viewer-password"})).json()["token"]
    anon.headers["authorization"] = f"Bearer {tok}"
    assert (await anon.get("/api/v1/system/status")).status_code == 200
    for method, path, body in [("post", "/api/v1/system/emergency-stop", {}), ("get", "/api/v1/secrets", None), ("get", "/api/v1/audit", None),
                               ("post", "/api/v1/businesses", {"name": "Nope", "idea": "not allowed"}),
                               ("post", "/api/v1/objectives", {"title": "Nope nope"})]:
        r = await getattr(anon, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code)

    # Secrets are write-only through the API.
    assert (await client.put("/api/v1/secrets", json={"name": "GITHUB_TOKEN", "value": "ghp_supersecret"})).status_code == 200
    listing = (await client.get("/api/v1/secrets")).text
    assert "GITHUB_TOKEN" in listing and "supersecret" not in listing
    integ = {i["name"]: i for i in (await client.get("/api/v1/tools")).json()["integrations"]}
    assert integ["github"]["configured"] and not integ["slack"]["configured"]
    audit = (await client.get("/api/v1/audit")).json()["entries"]
    assert {"auth.setup", "user.create", "secret.put"} <= {e["action"] for e in audit} and "supersecret" not in str(audit)
    assert any(e["action"] == "auth.login" and e["outcome"] == "denied" for e in audit)
    assert (await client.get("/api/v1/audit/verify")).json()["ok"]


async def test_full_flow_over_http(api):
    client, k = api

    def script(model, system, messages, tools):
        names, title = tool_names(tools), task_title(messages)
        if "plan_submit" in names and not called(messages, "plan_submit"):
            return call("plan_submit", tasks=[{"key": "app", "title": "Build the app", "description": "Write app.py and run it",
                                               "role": "full_stack_developer"}])
        if "objective_complete" in names:
            return call("objective_complete", summary="app.py prints the greeting.", achieved=True, score=1)
        if title == "Build the app":
            if not called(messages, "fs_write"):
                return call("fs_write", path="app.py", content="print('hello from aios')\n")
            if not called(messages, "code_execute"):
                return call("code_execute", language="python", code="exec(open('app.py').read())")
            return say("app.py written and executed: " + messages[-1]["results"][0]["content"].replace("\n", " "))
        return say("ok")

    ScriptedProvider.responder = staticmethod(script)
    biz = (await client.post("/api/v1/businesses", json={"name": "Hello Inc", "idea": "A greeting service", "kickoff": False})).json()
    proj = (await client.post("/api/v1/projects", json={"name": "Greeter", "kind": "api", "business_id": biz["id"],
                                                         "description": "Prints a greeting", "requirements": "Must print hello"})).json()

    async def delivered():
        d = (await client.get(f"/api/v1/projects/{proj['id']}")).json()
        objs = d["progress"]["objectives"]
        return d if objs and objs[0]["status"] in {"completed", "needs_attention"} else None

    detail = await wait_for(delivered, 30)
    assert detail["progress"]["objectives"][0]["status"] == "completed", detail["tasks"]
    assert detail["progress"]["percent"] == 100
    build = next(t for t in detail["tasks"] if t["title"] == "Build the app")
    assert "hello from aios" in build["result"]  # the program really ran in the sandbox
    assert "app.py" in {d["path"] for d in detail["deliverables"]}
    assert any("Build the app" in c["message"] for c in detail["history"])

    files = (await client.get(f"/api/v1/workspaces/{proj['namespace_id']}/files")).json()["entries"]
    assert {"app.py", "README.md", "docs"} <= {f["name"] for f in files}
    content = (await client.get(f"/api/v1/workspaces/{proj['namespace_id']}/file", params={"path": "app.py"})).json()["content"]
    assert content == "print('hello from aios')\n"
    assert (await client.get(f"/api/v1/workspaces/{proj['namespace_id']}/file", params={"path": "../../master.key"})).status_code == 400

    task = (await client.get(f"/api/v1/tasks/{build['id']}")).json()
    run = (await client.get(f"/api/v1/runs/{task['runs'][0]['id']}")).json()
    assert [m["role"] for m in run["messages"]] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]

    status = (await client.get("/api/v1/system/status")).json()
    assert status["counts"]["agents_active"] >= 8 and status["usage"]["llm_calls_today"] >= 5 and status["usage"]["exec_runs_today"] == 1
    assert status["workers"][0]["online"] and status["config"]["deployment_mode"] == "local"
    overview = (await client.get(f"/api/v1/businesses/{biz['id']}")).json()
    assert overview["projects"] == 1 and overview["projects_list"][0]["progress"]["percent"] == 100
    kinds = {e["kind"] for e in (await client.get("/api/v1/events", params={"limit": 500})).json()["events"]}
    assert {"business.created", "project.created", "objective.planned", "task.completed", "tool.call", "deliverable.updated",
            "objective.completed", "agent.created"} <= kinds
    chart = (await client.get("/api/v1/workforce/org-chart", params={"namespace_id": biz["namespace_id"]})).json()
    assert chart["roots"][0]["role"] == "ceo" and "Development" in chart["departments"]

    # Emergency stop over the API, then resume.
    assert (await client.post("/api/v1/system/emergency-stop", json={"reason": "test"})).json()["active"] is True
    assert (await client.get("/api/v1/system/status")).json()["emergency_stop"]["active"] is True
    assert (await client.post("/api/v1/system/resume")).json()["active"] is False

    # Finance, KPIs, customers.
    b = biz["id"]
    await client.post(f"/api/v1/businesses/{b}/ledger", json={"kind": "revenue", "amount": 1200, "category": "subscriptions"})
    await client.post(f"/api/v1/businesses/{b}/ledger", json={"kind": "expense", "amount": 300, "category": "hosting"})
    assert (await client.get(f"/api/v1/businesses/{b}/ledger")).json()["summary"]["net"] == 900
    await client.put(f"/api/v1/businesses/{b}/kpis", json={"key": "signups", "name": "Signups", "target": 100})
    await client.post(f"/api/v1/businesses/{b}/kpis/signups", json={"value": 42})
    assert (await client.get(f"/api/v1/businesses/{b}/kpis")).json()["kpis"][0]["latest"] == 42
    await client.put(f"/api/v1/businesses/{b}/customers", json={"name": "Dana", "email": "dana@example.com", "stage": "customer"})
    assert (await client.get(f"/api/v1/businesses/{b}/customers")).json()["customers"][0]["name"] == "Dana"

    # Backup: one archive with a consistent database snapshot and the workspaces, without the master key.
    info = (await client.post("/api/v1/system/backups", json={})).json()
    with tarfile.open(info["path"]) as tar:
        names = tar.getnames()
    assert ("aios.db" in names or "aios.pgdump" in names) and "manifest.json" in names and "master.key" not in names
    assert any(n.endswith("app.py") for n in names)


async def test_backup_restore_round_trip(api, tmp_path):
    client, k = api
    if not k.settings.is_sqlite:
        pytest.skip("file-level restore is the SQLite path; PostgreSQL backups are pg_dump archives restored with pg_restore")
    from aios_kernel import backup
    await client.post("/api/v1/memory", json={"content": "The launch date is the ninth of March", "title": "Launch"})
    ws = k.workspace_for("ns_root")
    (ws / "notes.txt").write_text("keep me")
    info = (await client.post("/api/v1/system/backups", json={"include_key": True})).json()

    restored = Settings(data_dir=tmp_path / "restored", embedded_worker=False)
    restored.finalize()
    note = backup.restore_backup_sync(restored, Path(info["path"]))
    assert "restored" in note["note"]
    from aios_api.system import build
    k2 = build(restored)
    await k2.start(run_worker=False)
    try:
        assert any("ninth of March" in h["content"] for h in await k2.memory.search("launch date", namespace_id="ns_root"))
        assert (k2.workspace_for("ns_root") / "notes.txt").read_text() == "keep me"
        assert (await k2.auth.login("owner@example.com", "owner-password"))[1].role == "owner"
    finally:
        await k2.stop()


def _imports(package: str) -> set[str]:
    found = set()
    for path in (SRC / package).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
    return found


def test_layer_boundaries():
    """Dependencies point strictly downward: API → ecosystem → workforce → kernel."""
    assert not {"aios_workforce", "aios_ecosystem", "aios_api"} & _imports("aios_kernel")
    assert not {"aios_ecosystem", "aios_api"} & _imports("aios_workforce")
    assert "aios_api" not in _imports("aios_ecosystem")
    # No cloud vendor SDK or broker is a hard dependency of any layer.
    everything = _imports("aios_kernel") | _imports("aios_workforce") | _imports("aios_ecosystem") | _imports("aios_api")
    assert not {"boto3", "botocore", "google", "azure", "redis", "celery", "kafka"} & everything
