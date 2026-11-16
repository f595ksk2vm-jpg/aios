"""Layer 2 collaboration: delegation, consultation, review loops, attenuated hiring, initiatives; plus hybrid placement."""
from aios_kernel.llm.scripted import ScriptedProvider
from aios_kernel.models import ROOT_NS, Job
from aios_kernel.tasks.queue import Worker
from sqlalchemy import select

from conftest import call, called, objective_done, say, task_title, tool_names, wait_for


async def org(k, preset="lean", **kw):
    eco = k.extensions["ecosystem"]
    biz = await eco.create_business(name="Acme", idea="widgets", created_by="user:test", org_preset=preset, kickoff=False, **kw)
    agents = {a.role: a for a in await k.agents.list(namespace_ids=[biz.namespace_id])}
    return biz, agents


async def task_done(k, task_id, status="done"):
    async def check():
        t = await k.orchestrator.get_task(task_id)
        return t if t.status in {status, "failed"} else None
    t = await wait_for(check)
    assert t.status == status, (t.status, t.error)
    return t


async def test_delegation_runs_in_parallel_and_reports_back(system):
    k = system
    biz, agents = await org(k)

    def script(model, system_prompt, messages, tools):
        title = task_title(messages)
        if title == "Ship the feature":
            if not called(messages, "agent_delegate"):
                # Two delegations in one turn: both children run concurrently.
                r = call("agent_delegate", to="full_stack_developer", title="Build it", instructions="Write feature.py")
                r.tool_calls += call("agent_delegate", to="qa_engineer", title="Test plan", instructions="Write tests.md").tool_calls
                return r
            if "Work you delegated has finished" in messages[-1].get("content", ""):
                return say("Shipped. " + ("child results received" if "feature.py written" in messages[-1]["content"] else "MISSING"))
            return say("Waiting on my team.")  # tries to finish early: the runtime holds the task open
        if title == "Build it":
            return say("feature.py written") if called(messages, "fs_write") else call("fs_write", path="feature.py", content="print('hi')\n")
        if title == "Test plan":
            return say("tests.md written") if called(messages, "fs_write") else call("fs_write", path="tests.md", content="# tests\n")
        return say("ok")

    ScriptedProvider.responder = staticmethod(script)
    parent = await k.orchestrator.create_task(namespace_id=biz.namespace_id, title="Ship the feature", agent_id=agents["cto"].id)
    t = await task_done(k, parent.id)
    assert t.result == "Shipped. child results received"
    children = [c for c in await k.orchestrator.list_tasks(namespace_ids=[biz.namespace_id]) if c.parent_task_id == parent.id]
    assert {c.assigned_agent_id for c in children} == {agents["full_stack_developer"].id, agents["qa_engineer"].id}
    assert all(c.status == "done" and c.meta["delegated_by"] == agents["cto"].id for c in children)
    kinds = [m.kind for m in await k.messaging.list(namespace_ids=[biz.namespace_id])]
    assert kinds.count("delegation") == 2


async def test_delegation_respects_hierarchy_and_consultation_works(system):
    k = system
    biz, agents = await org(k)
    seen = {}

    def script(model, system_prompt, messages, tools):
        title = task_title(messages)
        if title == "Decide stack":
            if not called(messages, "agent_delegate"):
                return call("agent_delegate", to=agents["ceo"].id, title="Do my job", instructions="please")
            if not called(messages, "agent_ask"):
                seen["delegate_result"] = messages[-1]["results"][0]
                return call("agent_ask", to="market_researcher", question="Is the bakery market large enough?")
            seen["answer"] = messages[-1]["results"][0]["content"]
            return say("decided")
        if "asks for your input" in messages[0]["content"]:
            seen["consulted_as"] = system_prompt.split("# Your position")[1][:120]
            return say("I disagree with the premise: the market is small but underserved.")
        return say("ok")

    ScriptedProvider.responder = staticmethod(script)
    task = await k.orchestrator.create_task(namespace_id=biz.namespace_id, title="Decide stack", agent_id=agents["cto"].id)
    await task_done(k, task.id)
    assert seen["delegate_result"]["is_error"] and "does not report to you" in seen["delegate_result"]["content"]
    assert "Market Researcher" in seen["consulted_as"]
    assert "I disagree with the premise" in seen["answer"]
    msgs = await k.messaging.list(namespace_ids=[biz.namespace_id])
    assert {"question", "answer"} <= {m.kind for m in msgs} and all(m.read_at for m in msgs if m.kind in {"question", "answer"})


async def test_review_requests_changes_then_approves(system):
    k = system
    biz, agents = await org(k)
    reviews = {"n": 0}

    def script(model, system_prompt, messages, tools):
        names, title = tool_names(tools), task_title(messages)
        if "plan_submit" in names and not called(messages, "plan_submit"):
            return call("plan_submit", tasks=[{"key": "api", "title": "Build the API", "description": "Write api.py",
                                               "role": "full_stack_developer", "needs_review": True}])
        if "objective_complete" in names:
            return call("objective_complete", summary="API built and reviewed.", achieved=True, score=1)
        if "review_submit" in names:
            reviews["n"] += 1
            if reviews["n"] == 1:
                return call("review_submit", verdict="request_changes", score=0.3, comments="No input validation.")
            return call("review_submit", verdict="approve", score=0.9, comments="Validation added.")
        if title == "Build the API":
            if not called(messages, "fs_write"):
                fixed = "No input validation" in messages[0]["content"]  # the reviewer's feedback is in the retry brief
                return call("fs_write", path="api.py", content="VALIDATED = True\n" if fixed else "VALIDATED = False\n")
            return say("api.py written")
        return say("ok")

    ScriptedProvider.responder = staticmethod(script)
    obj = await k.orchestrator.create_objective(namespace_id=biz.namespace_id, title="API", created_by="user:test")
    _, tasks = await objective_done(k, obj.id)
    api = next(t for t in tasks if t.title == "Build the API")
    assert api.attempts == 2 and api.meta["review"]["verdict"] == "approve" and reviews["n"] == 2
    assert (k.workspace_for(biz.namespace_id) / "api.py").read_text() == "VALIDATED = True\n"
    # The developer's work in the Development department was reviewed by QA, not by the author.
    assert {t.assigned_agent_id for t in tasks if t.kind == "review"} == {agents["qa_engineer"].id}


async def test_agent_hiring_is_attenuated_and_gated(system):
    k = system
    biz, agents = await org(k, autonomy="supervised")
    ScriptedProvider.responder = staticmethod(
        lambda m, s, msgs, tools: say("hired: " + msgs[-1]["results"][0]["content"]) if called(msgs, "agent_create")
        else call("agent_create", role="security engineer", focus="Focus on the payments flow."))
    task = await k.orchestrator.create_task(namespace_id=biz.namespace_id, title="Hire security", agent_id=agents["ceo"].id)

    # Supervised: creating an agent needs a human.
    async def waiting():
        t = await k.orchestrator.get_task(task.id)
        return t if t.status == "awaiting_approval" else None
    await wait_for(waiting)
    apr = (await k.approvals.list("pending"))[0]
    assert apr.action == "agent_create" and apr.category == "agent_mgmt"
    await k.approvals.decide(apr.id, approve=True, decided_by="usr_h")
    await k.orchestrator.on_approval_decided(apr.run_id)
    await task_done(k, task.id)

    hire = next(a for a in await k.agents.list(namespace_ids=[biz.namespace_id]) if a.role == "security_engineer")
    ceo = await k.agents.get(agents["ceo"].id)
    assert hire.created_by == f"agent:{ceo.id}" and "payments flow" in hire.system_prompt
    # exec.code and db.query are in the template's kit but the CEO does not hold them, so the hire does not get them.
    assert "exec.code" not in hire.permissions and "db.query" not in hire.permissions and "fs.*" in hire.permissions
    from aios_kernel.security import policy
    assert all(policy.covered_by(p, ceo.permissions) for p in hire.permissions)
    assert policy.autonomy_rank(hire.autonomy) <= policy.autonomy_rank(ceo.autonomy)


async def test_strategic_agent_launches_initiative_within_bounds(system):
    k = system
    biz, agents = await org(k, preset="minimal", autonomy="strategic")

    def script(model, system_prompt, messages, tools):
        names, title = tool_names(tools), task_title(messages)
        if "plan_submit" in names and not called(messages, "plan_submit"):
            return call("plan_submit", tasks=[{"key": "x", "title": "Pilot", "description": "Run the pilot"}])
        if "objective_complete" in names:
            return call("objective_complete", summary="Pilot done.", achieved=True)
        if title == "Quarterly review" and not called(messages, "initiative_propose"):
            return call("initiative_propose", title="Launch referral pilot", description="Offer referral credit",
                        rationale="Acquisition cost is rising")
        return say("ok")

    ScriptedProvider.responder = staticmethod(script)
    task = await k.orchestrator.create_task(namespace_id=biz.namespace_id, title="Quarterly review", agent_id=agents["ceo"].id)
    await task_done(k, task.id)
    objs = await k.orchestrator.list_objectives([biz.namespace_id])
    assert len(objs) == 1 and objs[0].created_by == f"agent:{agents['ceo'].id}" and objs[0].meta["initiative"]
    await objective_done(k, objs[0].id)
    assert not await k.approvals.list("pending")  # strategic autonomy: no human gate for initiatives


async def test_hybrid_placement_routes_jobs_and_models(system):
    k = system
    eco = k.extensions["ecosystem"]
    ScriptedProvider.responder = staticmethod(lambda *a: say("ok"))
    # The fixture's worker is "local". Stop it and run one local and one cloud worker against the same control plane.
    await k.worker.stop()
    local = Worker(k, location="local", concurrency=2, name="edge-box", poll_s=0.05)
    cloud = Worker(k, location="cloud", concurrency=2, name="cloud-pool", poll_s=0.05)
    ran: dict[str, str] = {}
    original = k.orchestrator.handle_task_run

    def tracking(worker):
        async def handler(payload):
            ran[payload["task_id"]] = worker.id
            await original(payload)
        return handler

    # Each worker gets its own handler table so we can see who executed what.
    local._execute, cloud._execute = _bind(local, tracking(local)), _bind(cloud, tracking(cloud))
    local.start(), cloud.start()
    try:
        private = await eco.create_business(name="Clinic", idea="patient data", created_by="user:t", org_preset="minimal",
                                            kickoff=False, compute="local", models="local")
        public = await eco.create_business(name="Blog", idea="content", created_by="user:t", org_preset="minimal",
                                           kickoff=False, compute="cloud")
        ceo_private = (await k.agents.list(namespace_ids=[private.namespace_id]))[0]
        ceo_public = (await k.agents.list(namespace_ids=[public.namespace_id]))[0]
        t1 = await k.orchestrator.create_task(namespace_id=private.namespace_id, title="Local job", agent_id=ceo_private.id)
        t2 = await k.orchestrator.create_task(namespace_id=public.namespace_id, title="Cloud job", agent_id=ceo_public.id)
        async with k.db.session() as s:
            affinity = {j.payload["task_id"]: j.affinity for j in (await s.execute(select(Job))).scalars()}
        assert affinity[t1.id] == "local" and affinity[t2.id] == "cloud"
        await task_done(k, t1.id), await task_done(k, t2.id)
        assert ran[t1.id] == "edge-box" and ran[t2.id] == "cloud-pool"

        # Data residency: a "local models only" namespace never routes to a cloud provider, even a pinned one.
        p = await k.models.upsert_provider(name="cloudy", kind="openai_compat", location="cloud", base_url="http://cloud.invalid/v1")
        await k.models.upsert_model(provider_id=p.id, model_name="big", tier="deep")
        _, placement = await k.namespaces.placement(private.namespace_id)
        cands = await k.models.candidates({"mode": "pinned", "model": "cloudy/big"}, models_placement=placement)
        assert placement == "local" and cands and all(c.provider.location == "local" for c in cands)
        cands = await k.models.candidates({"mode": "pinned", "model": "cloudy/big"}, models_placement="any")
        assert cands[0].ref == "cloudy/big"
        # A project inherits its business's placement and cannot loosen it.
        proj = await eco.create_project(name="Records", business_id=private.id, created_by="user:t", kickoff=False)
        assert await k.namespaces.placement(proj.namespace_id) == ("local", "local")
        # Both workers are visible to the control plane.
        from aios_kernel.models import WorkerNode
        async with k.db.session() as s:
            nodes = {w.id: w.location for w in (await s.execute(select(WorkerNode))).scalars()}
        assert nodes.get("edge-box") == "local" and nodes.get("cloud-pool") == "cloud"
    finally:
        await local.stop(), await cloud.stop()
        k.worker = None


def _bind(worker, handler):
    async def execute(job):
        try:
            if job.kind == "task.run":
                await handler({**job.payload, "_attempt": job.attempts + 1})
            else:
                await worker.k.job_handlers[job.kind]({**job.payload, "_attempt": job.attempts + 1})
            await worker.k.queue.finish(job.id)
        except Exception as e:  # pragma: no cover - surfaced through task state
            await worker.k.queue.finish(job.id, error=str(e))
        finally:
            worker.k.queue.wake.set()
    return execute
