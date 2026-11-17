import asyncio
import sys
from harbor.checks import run_checks
from harbor.config.workflow import load_workflow
from test_workflow_config import config


def test_parallel_collects_failures_and_blocks_dependencies(tmp_path):
    workflow = load_workflow(config(tmp_path))
    workflow.config["checks"] = {
        "good":{"command":[sys.executable,"-c","pass"]},
        "bad":{"command":[sys.executable,"-c","raise SystemExit(2)"]},
        "dependent":{"command":[sys.executable,"-c","raise AssertionError('must not run')"],"after":["bad"]},
    }
    result = asyncio.run(run_checks(workflow,{"project":"A","ticket":"A-1","run_id":"r"},cwd=tmp_path,parallel=True))
    assert result["status"] == "fail"
    assert result["checks"]["good"]["status"] == "pass"
    assert result["checks"]["dependent"]["status"] == "blocked"
