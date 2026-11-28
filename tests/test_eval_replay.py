import json
from pathlib import Path

from evals.run_eval import run  # noqa: E402


async def test_eval_set_has_enough_questions():
    qs = json.loads((Path(__file__).parent.parent / "evals" / "questions.json").read_text("utf-8"))
    assert len(qs) >= 15
    assert len({q["id"] for q in qs}) == len(qs)


async def test_all_eval_questions_pass_offline():
    assert await run("replay") == 0
