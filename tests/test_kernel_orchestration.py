"""Layer 1 on its own: objective → plan → parallel task graph → finalization."""
from aios_kernel.llm.scripted import ScriptedProvider
from aios_kernel.models import ROOT_NS

from conftest import call, called, say, tool_names, wait_for


def planner_and_workers(model, system, messages, tools):
    names = tool_names(tools)
    if "plan_submit" in names and not called(messages, "plan_submit"):
        return call("plan_submit", summary="two parallel parts then a merge", tasks=[
            {"key": "a", "title": "Write part A", "description": "Write a.txt containing ALPHA"},
            {"key": "b", "title": "Write part B", "description": "Write b.txt containing BETA"},
            {"key": "c", "title": "Merge parts", "description": "Concatenate a.txt and b.txt into merged.txt using code", "depends_on": ["a", "b"]},
        ])
    if "objective_complete" in names and not called(messages, "objective_complete"):
        return call("objective_complete", summary="Merged file produced at merged.txt", achieved=True, score=0.9,
                    lessons=["Splitting independent parts lets them run in parallel."])
    brief = messages[0]["content"].splitlines()[0]
    if "Write part A" in brief:
        return say("wrote a.txt") if called(messages, "fs_write") else call("fs_write", path="a.txt", content="ALPHA\n")
    if "Write part B" in brief:
        return say("wrote b.txt") if called(messages, "fs_write") else call("fs_write", path="b.txt", content="BETA\n")
    if "Merge parts" in brief:
        if not called(messages, "code_execute"):
            return call("code_execute", language="python",
                        code="open('merged.txt','w').write(open('a.txt').read()+open('b.txt').read()); print('merged ok')")
        return say("merged.txt written; verified by execution")
    return say("done")


async def test_objective_runs_to_completion(kernel):
    k = kernel
    ScriptedProvider.responder = staticmethod(planner_and_workers)
    atlas = (await k.agents.list())[0]
    await k.agents.update(atlas.id, tools=["fs_read", "fs_write", "fs_list", "code_execute", "memory_search"],
                          permissions=["fs.*", "exec.code", "memory.*"])
    obj = await k.orchestrator.create_objective(namespace_id=ROOT_NS, title="Produce merged file", description="A and B merged")

    async def finished():
        o = await k.orchestrator.get_objective(obj.id)
        return o if o.status in {"completed", "needs_attention", "failed"} else None

    o = await wait_for(finished)
    tasks = await k.orchestrator.list_tasks(objective_id=obj.id)
    assert o.status == "completed", [(t.title, t.status, t.error) for t in tasks]
    assert o.evaluation["achieved"] is True and o.evaluation["score"] == 0.9
    assert {t.kind for t in tasks} == {"plan", "work", "finalize"}
    assert all(t.status == "done" for t in tasks)

    # Real effects: the files exist and the merge was produced by actually running code in the sandbox.
    ws = k.workspace_for(ROOT_NS)
    assert (ws / "merged.txt").read_text() == "ALPHA\nBETA\n"

    # The merge task received its prerequisites' results in its brief.
    merge = next(t for t in tasks if t.title == "Merge parts")
    run = (await k.orchestrator.runs_for(merge.id))[0]
    assert "wrote a.txt" in run.messages[0]["content"] and "wrote b.txt" in run.messages[0]["content"]

    # Usage was metered, results became memory, the audit chain is intact.
    usage = await k.budget.summary()
    assert usage["calls"] >= 6
    hits = await k.memory.search("merged file", namespace_id=ROOT_NS)
    assert any("merged" in h["content"].lower() for h in hits)
    assert (await k.audit.verify())["ok"]

