import unittest

from gan.framework.context import EvalContext, reset_eval_context, set_eval_context
from gan.framework.role_process import _validated_result
from gan.tools.work.evaluator import submit_challenge


class SubmitChallengeTest(unittest.TestCase):
    def _ctx(self, *, enabled=True, revealed=False):
        ctx = EvalContext()
        ctx.configure_challenges(
            domain="imo_proof", allowed_qids=["q1", "q2"], max_k=2,
            blind_only=enabled,
        )
        ctx.revealed = revealed
        return ctx

    def test_blind_train_challenge_is_recorded_and_bounded(self):
        ctx = self._ctx()
        token = set_eval_context(ctx)
        try:
            result = submit_challenge.tool_function(
                "q1", "missing_lemma", "lemma absent", "proof:2",
                "lemma stated and used")
            self.assertIn("recorded", result)
            self.assertEqual(ctx.challenges[0]["qid"], "q1")
            duplicate = submit_challenge.tool_function(
                "q1", "missing_lemma", "again", "proof:2", "fixed")
            self.assertIn("duplicate", duplicate)
        finally:
            reset_eval_context(token)

    def test_rejects_val_qid_and_revealed_submission(self):
        ctx = self._ctx()
        token = set_eval_context(ctx)
        try:
            result = submit_challenge.tool_function(
                "val-1", "missing_lemma", "bad", "proof:2", "fixed")
            self.assertIn("allowed train", result)
        finally:
            reset_eval_context(token)
        ctx = self._ctx(revealed=True)
        token = set_eval_context(ctx)
        try:
            result = submit_challenge.tool_function(
                "q1", "missing_lemma", "bad", "proof:2", "fixed")
            self.assertIn("blind phase", result)
        finally:
            reset_eval_context(token)

    def test_legacy_and_new_evaluator_ipc_results_are_accepted(self):
        legacy = {
            "predicted_score": None, "issues": [], "fix_verdicts": [],
            "penalties": {}, "eval_point_results": [], "weaknesses": [],
            "token_budget_exhausted": False, "truncated": False,
        }
        current = {**legacy, "challenges": []}
        self.assertEqual(_validated_result("evaluate", legacy).challenges, [])
        self.assertEqual(_validated_result("evaluate", current).challenges, [])

    def test_legacy_planner_ipc_result_is_accepted(self):
        legacy = {
            "records": [], "responses": [], "config": {}, "patch": "",
            "patch_proposed": "", "patch_rejection": None, "attempts": 1,
            "truncated": False, "budget_exhausted": False,
            "token_budget_exhausted": False,
        }
        result = _validated_result("plan", legacy)
        self.assertFalse(result["deep_probe_required"])
        self.assertFalse(result["deep_probe_patch_proposed"])


if __name__ == "__main__":
    unittest.main()
