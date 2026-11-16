"""Security & control: the guarantees agents cannot talk their way around."""
import pytest
from sqlalchemy import select

from aios_kernel.errors import BudgetExceeded, PermissionDenied
from aios_kernel.llm.scripted import ScriptedProvider
from aios_kernel.llm.types import ToolCall
from aios_kernel.models import ROOT_NS, AuditEntry, Secret
from aios_kernel.security import policy
from aios_kernel.security.auth import verify_password
from aios_kernel.tools.base import ToolContext

from conftest import call, called, say, wait_for


async def make_agent(k, **kw):
    spec = dict(namespace_id=ROOT_NS, name="Worker", role="tester", tools=["fs_read", "fs_write", "code_execute"],
                permissions=["fs.*", "exec.code"], autonomy="supervised")
    spec.update(kw)
    return await k.agents.create(**spec)


def ctx_for(k, agent, ns=ROOT_NS):
    return ToolContext(kernel=k, agent=agent, namespace_id=ns, workspace=k.workspace_for(ns))


async def invoke(k, agent, name, **args):
    return await k.tools.invoke(ctx_for(k, agent), ToolCall("c1", name, args), run_tools=[])


async def test_permission_is_enforced_by_the_gateway_and_audited(kernel):
    k = kernel
    # The tool is listed for the agent, but the permission that authorises it is not held.
    agent = await make_agent(k, tools=["fs_write", "http_request"], permissions=["fs.read"])
    out = await invoke(k, agent, "fs_write", path="x.txt", content="nope")
    assert out.status == "denied" and not (k.workspace_for(ROOT_NS) / "x.txt").exists()
    # A tool that was never given to the agent is refused even if a permission pattern would match.
    wide = await make_agent(k, name="Wide", tools=[], permissions=["*"])
    assert (await invoke(k, wide, "fs_write", path="y.txt", content="nope")).status == "denied"
    async with k.db.session() as s:
        denied = list((await s.execute(select(AuditEntry).where(AuditEntry.outcome == "denied"))).scalars())
    assert len(denied) == 2 and denied[0].action == "tool.fs_write"


async def test_workspace_confinement(kernel):
    k = kernel
    agent = await make_agent(k)
    ws = k.workspace_for(ROOT_NS)
    for bad in ["../../etc/passwd", "a/../../../x", ".git/config"]:
        out = await invoke(k, agent, "fs_write", path=bad, content="x")
        assert out.status == "error", bad
    assert (await invoke(k, agent, "fs_read", path="../../../etc/passwd")).status == "error"
    # Absolute paths are interpreted inside the workspace, never on the host.
    assert (await invoke(k, agent, "fs_write", path="/etc/hosts", content="mine")).status == "ok"
    assert (ws / "etc" / "hosts").read_text() == "mine"
    # A symlink planted in the workspace cannot be used to step outside it.
    (ws / "escape").symlink_to("/etc")
    assert (await invoke(k, agent, "fs_read", path="escape/passwd")).status == "error"
    assert (await invoke(k, agent, "fs_write", path="ok/deep/file.txt", content="fine")).status == "ok"


async def test_sandbox_limits(kernel):
    k = kernel
    agent = await make_agent(k)
    net = await invoke(k, agent, "code_execute", language="python",
                       code="import socket\ntry:\n    socket.create_connection(('1.1.1.1', 53), 3); print('CONNECTED')\nexcept OSError as e:\n    print('BLOCKED', e)")
    assert "BLOCKED" in net.content and "CONNECTED" not in net.content
    slow = await invoke(k, agent, "code_execute", language="bash", code="sleep 30", timeout_s=1)
    assert "timed out" in slow.content
    env = await invoke(k, agent, "code_execute", language="bash", code="env")
    assert "AIOS_" not in env.content and "ANTHROPIC" not in env.content  # host secrets are not inherited
    # network=true is an external action: a supervised agent needs a human for it.
    assert (await invoke(k, agent, "code_execute", language="bash", code="true", network=True)).status == "needs_approval"


async def test_autonomy_policy_matrix():
    assert policy.decide("assisted", "write_internal") == "approve"
    assert policy.decide("supervised", "write_internal") == "allow"
    assert policy.decide("supervised", "external_comm") == "approve"
    assert policy.decide("autonomous", "external_comm") == "allow"
    assert policy.decide("autonomous", "initiative") == "approve" and policy.decide("strategic", "initiative") == "allow"
    for level in policy.AUTONOMY_LEVELS:  # no level ever acts alone on these
        for cat in ("financial", "legal", "sensitive_data"):
            assert policy.decide(level, cat) == "approve"
        assert policy.decide(level, "destructive") == "approve"


async def test_agents_cannot_escalate_privileges(kernel):
    k = kernel
    boss = await make_agent(k, name="Boss", tools=["fs_read"], permissions=["fs.read", "agents.create", "memory.*"], autonomy="supervised")
    # Cannot mint permissions it does not hold, nor a wildcard broader than its own.
    for perms in (["fs.write"], ["*"], ["fs.*"], ["exec.code"]):
        with pytest.raises(PermissionDenied):
            await k.agents.create_by_agent(boss, name="Minion", role="x", permissions=perms)
    with pytest.raises(PermissionDenied):
        await k.agents.create_by_agent(boss, name="Minion", role="x", permissions=["fs.read"], autonomy="autonomous")
    ok = await k.agents.create_by_agent(boss, name="Minion", role="x", permissions=["fs.read", "memory.read"])
    assert ok.manager_id == boss.id and ok.created_by == f"agent:{boss.id}"
    # Without agents.create there is no path at all.
    plain = await make_agent(k, name="Plain")
    with pytest.raises(PermissionDenied):
        await k.agents.create_by_agent(plain, name="Clone", role="x", permissions=[])
    # No registered tool can reach the reconfiguration API: it exists only on the human-authenticated control plane.
    assert not [t.name for t in k.tools.all() if "permission" in t.name or t.name in {"agent_update", "agent_configure"}]
    assert policy.covered_by("memory.read", ["memory.*"]) and not policy.covered_by("memory.*", ["memory.read"])


def approval_script(model, system, messages, tools):
    if not called(messages, "request_approval"):
        return call("request_approval", action="Sign the hosting contract", category="legal", justification="needed to launch")
    last = messages[-1]["results"][0] if messages[-1]["role"] == "tool" else None
    return say("PROCEEDING" if last and not last["is_error"] else "STOPPED: " + (last or {}).get("content", ""))


@pytest.mark.parametrize("approve", [True, False])
async def test_human_approval_suspends_and_resumes_a_run(kernel, approve):
    k = kernel
    ScriptedProvider.responder = staticmethod(approval_script)
    agent = await make_agent(k, tools=["request_approval"], permissions=["approvals.request"], autonomy="strategic")
    task = await k.orchestrator.create_task(namespace_id=ROOT_NS, title="Sign contract", agent_id=agent.id)

    async def waiting():
        t = await k.orchestrator.get_task(task.id)
        return t if t.status == "awaiting_approval" else None

    await wait_for(waiting)
    pending = await k.approvals.list("pending")
    assert len(pending) == 1 and pending[0].category == "legal" and pending[0].agent_id == agent.id
    run = (await k.orchestrator.runs_for(task.id))[0]
    assert run.status == "suspended" and run.pending["calls"][0]["approval_id"] == pending[0].id

    decided = await k.approvals.decide(pending[0].id, approve=approve, decided_by="usr_human", reason="" if approve else "too risky")
    await k.orchestrator.on_approval_decided(decided.run_id)

    async def done():
        t = await k.orchestrator.get_task(task.id)
        return t if t.status == "done" else None

    t = await wait_for(done)
    assert t.result.startswith("PROCEEDING") if approve else ("STOPPED" in t.result and "too risky" in t.result)


async def test_emergency_stop_halts_and_resume_continues(kernel):
    k = kernel
    steps = {"n": 0}

    def script(model, system, messages, tools):
        steps["n"] += 1
        if steps["n"] <= 3:
            return call("fs_write", path=f"step{steps['n']}.txt", content="x")
        return say("finished all steps")

    ScriptedProvider.responder = staticmethod(script)
    agent = await make_agent(k)
    await k.emergency_stop(by="user:test", reason="drill")
    task = await k.orchestrator.create_task(namespace_id=ROOT_NS, title="Three steps", agent_id=agent.id)
    import asyncio
    await asyncio.sleep(0.5)
    assert (await k.orchestrator.get_task(task.id)).status == "ready" and steps["n"] == 0  # workers claim nothing while stopped

    # Engage the stop mid-run: the run suspends at the next checkpoint with its progress intact.
    await k.resume_operations(by="user:test")

    def stop_midway(model, system, messages, tools):
        steps["n"] += 1
        if steps["n"] == 2:
            k.killswitch._cache = (__import__("time").monotonic(), {"active": True})  # as if another operator hit stop now
        if steps["n"] <= 3:
            return call("fs_write", path=f"step{steps['n']}.txt", content="x")
        return say("finished all steps")

    ScriptedProvider.responder = staticmethod(stop_midway)

    async def paused():
        t = await k.orchestrator.get_task(task.id)
        return t if t.status == "paused" else None

    await k.killswitch.set(False, by="t")
    await wait_for(paused)
    await k.killswitch.set(True, by="user:test", reason="drill")
    ws = k.workspace_for(ROOT_NS)
    assert (ws / "step1.txt").exists() and not (ws / "step3.txt").exists()

    ScriptedProvider.responder = staticmethod(script)
    out = await k.resume_operations(by="user:test")
    assert out["resumed_runs"] == 1

    async def done():
        t = await k.orchestrator.get_task(task.id)
        return t if t.status == "done" else None

    assert (await wait_for(done)).result == "finished all steps"
    assert (ws / "step2.txt").exists() and (ws / "step3.txt").exists()  # the interrupted call ran exactly once, after resume
    actions = [e.action for e in await k.audit.list()]
    assert "system.emergency_stop" in actions and "system.resume" in actions


async def test_spending_limit_stops_work(kernel):
    k = kernel
    ScriptedProvider.responder = staticmethod(lambda *a: say("ok"))
    agent = await make_agent(k)
    await k.budget.set_budget("agent", agent.id, "day", 0.01)
    await k.budget.record(namespace_id=ROOT_NS, agent_id=agent.id, kind="llm", provider="x", model="x", cost_usd=0.02)
    with pytest.raises(BudgetExceeded):
        await k.budget.check(ROOT_NS, agent.id)
    task = await k.orchestrator.create_task(namespace_id=ROOT_NS, title="Costly", agent_id=agent.id)

    async def failed():
        t = await k.orchestrator.get_task(task.id)
        return t if t.status == "failed" else None

    t = await wait_for(failed)
    assert "spending limit" in t.error
    # The failure escalated to the humans' feed (the agent has no manager).
    async def escalated():
        return any(m.kind == "escalation" and m.to_agent is None for m in await k.messaging.list())

    await wait_for(escalated)


async def test_secrets_and_auth(kernel):
    k = kernel
    await k.secrets.put("API_TOKEN", "s3cr3t-value", namespace_id=ROOT_NS)
    async with k.db.session() as s:
        row = (await s.execute(select(Secret))).scalar_one()
    assert "s3cr3t" not in row.ciphertext and "ciphertext" not in row.to_dict()
    ns = await k.namespaces.create("business", "Tenant")
    await k.secrets.put("API_TOKEN", "tenant-value", namespace_id=ns.id)
    assert await k.secrets.get("API_TOKEN", [ns.id, ROOT_NS]) == "tenant-value"  # nearest namespace wins
    assert await k.secrets.get("API_TOKEN", [ROOT_NS]) == "s3cr3t-value"

    user = await k.auth.create_user("Admin@Example.com", "correct horse", role="operator")
    assert verify_password("correct horse", user.password_hash) and "correct" not in user.password_hash
    token, _ = await k.auth.login("admin@example.com", "correct horse")
    principal = await k.auth.authenticate(token)
    assert principal.can("approvals.decide".replace(".", ":")) and not principal.can("secrets:manage")
    with pytest.raises(PermissionDenied):
        principal.require("system:control")
    await k.auth.revoke(token)
    with pytest.raises(Exception):
        await k.auth.authenticate(token)


async def test_audit_chain_detects_tampering(kernel):
    k = kernel
    for i in range(5):
        await k.audit.record("user:x", "thing.done", detail={"i": i})
    assert (await k.audit.verify())["ok"]
    async with k.db.session() as s:
        row = (await s.execute(select(AuditEntry).where(AuditEntry.action == "thing.done").limit(1))).scalar_one()
        row.detail = {"i": 999}
    v = await k.audit.verify()
    assert not v["ok"] and v["broken_at"] == row.id
