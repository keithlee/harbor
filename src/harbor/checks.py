"""Dependency-aware command groups; parallelism must be explicitly requested."""
import asyncio
from dataclasses import replace
from harbor.hooks import execute_hook


async def run_checks(workflow, context, *, cwd, parallel=False):
    checks = workflow.config.get("checks", {})
    if not checks:
        return {"status":"disabled","checks":{}}
    pending, results = dict(checks), {}
    while pending:
        ready = [name for name, spec in pending.items() if all(dep in results for dep in spec.get("after", []))]
        if not ready:
            raise ValueError("check dependency cycle or unknown dependency")
        async def run(name):
            spec = pending[name]
            if any(results[dep]["status"] != "pass" for dep in spec.get("after", [])):
                return {"hook":name,"status":"blocked","blocking":spec.get("required",True),"reason":"dependency failed"}
            hook = {"event":"setup","executor":"script","protocol":"exit_code","mode":"required" if spec.get("required",True) else "advisory", "command":spec["command"],"timeout_seconds":spec.get("timeout_seconds",300)}
            local = replace(workflow, config={**workflow.config,"hooks":{name:hook}})
            return await execute_hook(local,name,context,cwd=cwd)
        batch = ready if parallel else ready[:1]
        outcomes = await asyncio.gather(*(run(name) for name in batch))
        for name, result in zip(batch,outcomes):
            results[name] = result
            del pending[name]
    return {"status":"fail" if any(r["blocking"] for r in results.values()) else "pass", "checks":results}
