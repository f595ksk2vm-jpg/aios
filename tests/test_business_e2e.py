"""All three layers together: describe a business, the workforce assembles and delivers real artefacts."""
from sqlalchemy import select

from aios_kernel.llm.scripted import ScriptedProvider
from aios_kernel.tools import builtin

from conftest import call, called, objective_done, say, task_title, tool_names

DOCS = {
    "Research the market": ("docs/market-research.md", "# Market research\n\nSegment: indie bakeries. Estimate (not sourced): 20k shops.\n"),
    "Design the business model": ("docs/business-model.md", "# Business model\n\nSubscription, $29/month.\n"),
    "Write the strategy": ("docs/strategy.md", "# Strategy\n\nStart with one city.\n"),
}


def responder(model, system, messages, tools):
    names, title = tool_names(tools), task_title(messages)
    if "plan_submit" in names and not called(messages, "plan_submit"):
        return call("plan_submit", summary="research, model, then strategy", tasks=[
            {"key": "research", "title": "Research the market", "description": "Write docs/market-research.md", "role": "market_researcher",
             "needs_review": True},
            # No CFO exists in the lean preset: the factory must provision one on demand.
            {"key": "model", "title": "Design the business model", "description": "Write docs/business-model.md", "role": "cfo"},
            {"key": "strategy", "title": "Write the strategy", "description": "Write docs/strategy.md and record profile + KPIs",
             "role": "ceo", "depends_on": ["research", "model"]},
        ])
    if "objective_complete" in names and not called(messages, "objective_complete"):
        return call("objective_complete", summary="Foundation documents are in docs/.", achieved=True, score=0.8)
    if "review_submit" in names and not called(messages, "review_submit"):
        if not called(messages, "fs_read"):
            return call("fs_read", path="docs/market-research.md")  # a reviewer inspects the real artefact
        return call("review_submit", verdict="approve", score=0.85, comments="Estimates are labelled as estimates.")
    if title in DOCS:
        path, content = DOCS[title]
        if not called(messages, "fs_write"):
            return call("fs_write", path=path, content=content)
        if title == "Write the strategy":
            if not called(messages, "business_profile_update"):
                return call("business_profile_update", mission="Software for independent bakeries", stage="validation")
            if not called(messages, "kpi_define"):
                return call("kpi_define", key="mrr", name="Monthly recurring revenue", unit="USD", target=5000)
        return say(f"Wrote {path}.")
    return say("done")


async def test_business_from_idea(system):
    k = system
    eco, wf = k.extensions["ecosystem"], k.extensions["workforce"]
    ScriptedProvider.responder = staticmethod(responder)

    biz = await eco.create_business(name="Crumb", idea="Ordering software for independent bakeries", created_by="user:test",
                                    org_preset="lean", monthly_budget_usd=50)
    obj = (await k.orchestrator.list_objectives([biz.namespace_id]))[0]
    o, tasks = await objective_done(k, obj.id)

    # --- Layer 2: an organization was assembled, with hierarchy, and extended on demand -------------
    agents = await k.agents.list(namespace_ids=[biz.namespace_id])
    by_role = {a.role: a for a in agents}
    assert {"ceo", "cto", "project_manager", "full_stack_developer", "qa_engineer", "market_researcher", "marketing_strategist"} <= set(by_role)
    assert "cfo" in by_role and by_role["cfo"].created_by == "system:provisioner"
    assert by_role["cto"].manager_id == by_role["ceo"].id
    assert by_role["full_stack_developer"].manager_id == by_role["cto"].id  # no architect yet: next manager up the line
    chart = await wf.factory.org_chart(biz.namespace_id)
    assert chart["roots"][0]["role"] == "ceo" and chart["headcount"] == len(agents)

    # Tasks went to the right people and the reviewed task really was reviewed by someone else.
    work = {t.title: t for t in tasks if t.kind == "work"}
    assert work["Research the market"].assigned_agent_id == by_role["market_researcher"].id
    assert work["Design the business model"].assigned_agent_id == by_role["cfo"].id
    review = next(t for t in tasks if t.kind == "review")
    assert review.assigned_agent_id != by_role["market_researcher"].id and review.status == "done"
    assert work["Research the market"].meta["review"]["verdict"] == "approve"

    # --- Layer 3: real deliverables, versioned, attributed --------------------------------------------
    ws = k.workspace_for(biz.namespace_id)
    for path, content in DOCS.values():
        assert (ws / path).read_text() == content
    log = await builtin.git(ws, "log", "--format=%an|%s")
    assert by_role["market_researcher"].name + "|Research the market" in log
    delivered = {d.path: d for d in await eco.deliverables([biz.namespace_id])}
    assert set(p for p, _ in DOCS.values()) <= set(delivered)
    assert delivered["docs/strategy.md"].kind == "document" and delivered["docs/strategy.md"].agent_id == by_role["ceo"].id

    fresh = await eco.business(biz.id)
    assert fresh.mission == "Software for independent bakeries" and fresh.stage == "validation"
    assert [kpi["key"] for kpi in await eco.kpis(biz.id)] == ["mrr"]
    overview = await eco.overview(biz.id)
    assert overview["tasks"]["done"] == 3 and overview["agents"] == len(agents) and overview["ai_cost_usd"] >= 0

    # The CEO got reports from direct reports; performance metrics are computed from real records.
    inbox = await k.messaging.list(agent_id=by_role["ceo"].id)
    assert any(m.kind == "report" for m in inbox)
    perf = await wf.performance(by_role["market_researcher"].id)
    assert perf["tasks_done"] == 1 and perf["avg_review_score"] == 0.85 and perf["llm_calls"] >= 2


async def test_businesses_are_isolated(system):
    k = system
    eco = k.extensions["ecosystem"]
    a = await eco.create_business(name="Alpha", idea="a", created_by="user:test", org_preset="minimal", kickoff=False)
    b = await eco.create_business(name="Beta", idea="b", created_by="user:test", org_preset="minimal", kickoff=False)
    await k.memory.add(namespace_id=a.namespace_id, title="Alpha pricing", content="Alpha's secret launch price is 49 dollars")

    # Knowledge: invisible to the sibling until explicitly granted, then visible, then gone again on revoke.
    assert not [h for h in await k.memory.search("secret launch price", namespace_id=b.namespace_id) if "Alpha" in h["content"]]
    grant = await k.namespaces.grant(a.namespace_id, b.namespace_id, "knowledge", created_by="user:test")
    assert any("49 dollars" in h["content"] for h in await k.memory.search("secret launch price", namespace_id=b.namespace_id))
    await k.namespaces.revoke(grant.id)
    assert not [h for h in await k.memory.search("secret launch price", namespace_id=b.namespace_id) if "49" in h["content"]]

    # Agents: Beta cannot use Alpha's CEO; a single-agent grant shares exactly that agent.
    alpha_ceo = (await k.agents.list(namespace_ids=[a.namespace_id]))[0]
    assert alpha_ceo.id not in {x.id for x in await k.agents.usable(b.namespace_id)}
    await k.namespaces.grant(a.namespace_id, b.namespace_id, "agent", resource_id=alpha_ceo.id)
    assert alpha_ceo.id in {x.id for x in await k.agents.usable(b.namespace_id)}

    # Projects nest under their business: the business sees project knowledge, a sibling business does not.
    proj = await eco.create_project(name="Alpha App", kind="saas", business_id=a.id, created_by="user:test", kickoff=False)
    await k.memory.add(namespace_id=proj.namespace_id, title="Stack", content="Alpha App uses the zebra framework")
    assert any("zebra" in h["content"] for h in await k.memory.search("zebra framework", namespace_id=a.namespace_id))
    assert not any("zebra" in h["content"] for h in await k.memory.search("zebra framework", namespace_id=b.namespace_id))

    # Workspaces are separate directories and separate repositories.
    assert k.workspace_for(a.namespace_id) != k.workspace_for(proj.namespace_id)
    assert (k.workspace_for(proj.namespace_id) / "docs" / "REQUIREMENTS.md").exists()
    async with k.db.session() as s:
        from aios_ecosystem.models import Project
        assert (await s.execute(select(Project).where(Project.business_id == b.id))).first() is None
