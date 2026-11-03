import os
import tempfile
import unittest

try:
    import pandas as pd
    from gan.framework.task_execution import select_samples
except ImportError:  # lightweight local test environments may omit pandas
    pd = None
    select_samples = None

from gan.framework import challenge_ledger
from gan.framework.ledger import open_run


class ChallengeLedgerTest(unittest.TestCase):
    def setUp(self):
        self.data = challenge_ledger.empty()
        self.kwargs = {
            "outer": 1,
            "parent_node": "p0",
            "evaluator_version": "ev-1",
            "allowed_qids": ["q1", "q2", "q3"],
            "challenges": [{
                "qid": "q1", "defect_type": "missing_lemma",
                "claim": "lemma is absent", "evidence_ref": "proof:line-2",
                "repair_criterion": "lemma is stated and used",
            }],
        }

    def test_lock_is_blind_and_qid_bound(self):
        with self.assertRaisesRegex(challenge_ledger.ChallengeError, "revealed"):
            challenge_ledger.lock(
                self.data, **{**self.kwargs, "challenges": [{
                    **self.kwargs["challenges"][0], "score": 6,
                }]})
        with self.assertRaisesRegex(challenge_ledger.ChallengeError, "allowed train"):
            challenge_ledger.lock(
                self.data, **{**self.kwargs, "challenges": [{
                    **self.kwargs["challenges"][0], "qid": "val-1",
                }]})

    def test_lock_resume_is_idempotent_and_payload_is_immutable(self):
        ids = challenge_ledger.lock(self.data, **self.kwargs)
        self.assertEqual(ids, challenge_ledger.lock(self.data, **self.kwargs))
        changed = {**self.kwargs, "challenges": [{
            **self.kwargs["challenges"][0], "claim": "changed",
        }]}
        with self.assertRaisesRegex(challenge_ledger.ChallengeError, "does not match"):
            challenge_ledger.lock(self.data, **changed)

    def test_direct_child_reservation_and_one_time_consumption(self):
        ids = challenge_ledger.lock(self.data, **self.kwargs)
        available = challenge_ledger.available_for_parent(
            self.data, parent_node="p0")
        self.assertEqual([row["challenge_id"] for row in available], ids)
        rows = challenge_ledger.reserve_for_child(
            self.data, parent_node="p0", child_node="c1")
        self.assertEqual([row["challenge_id"] for row in rows], ids)
        with self.assertRaisesRegex(challenge_ledger.ChallengeError, "another child"):
            challenge_ledger.reserve_for_child(
                self.data, parent_node="p0", child_node="c2")
        consumed = challenge_ledger.consume_for_child(
            self.data, parent_node="p0", child_node="c1")
        self.assertEqual(consumed[0]["status"], challenge_ledger.STATUS_CONSUMED)
        self.assertEqual(
            challenge_ledger.consume_for_child(
                self.data, parent_node="p0", child_node="c1"), consumed)
        self.assertEqual(
            challenge_ledger.available_for_parent(self.data, parent_node="p0"),
            [])

    @unittest.skipUnless(os.name == "posix", "POSIX filesystem permissions required")
    def test_save_requires_framework_writer_and_load_round_trip(self):
        temp_parent = os.environ.get("GAN_TEST_TMPDIR") or os.getcwd()
        with tempfile.TemporaryDirectory(dir=temp_parent) as root:
            with self.assertRaises(TypeError):
                challenge_ledger.save(root, self.data, None)
            challenge_ledger.lock(self.data, **self.kwargs)
            challenge_ledger.save(root, self.data, open_run(root))
            loaded = challenge_ledger.load(root)
            self.assertEqual(loaded["challenges"], self.data["challenges"])

    @unittest.skipUnless(pd is not None, "pandas is required for sampling tests")
    def test_challenge_sampling_is_mandatory_and_separate_from_base_anchors(self):
        frame = pd.DataFrame({"qid": [f"q{i}" for i in range(8)], "answer": ["x"] * 8})
        selected, info = select_samples(
            frame, "qid", 5, sample_seed=7,
            anchor_ids=["q0", "q1"], challenge_ids=["q5", "q1"])
        selected_ids = selected["qid"].tolist()
        self.assertEqual(info["anchor_ids"], ["q0", "q1"])
        self.assertEqual(info["challenge_ids"], ["q1", "q5"])
        self.assertEqual(set(info["sampled_ids"]), set(selected_ids))
        self.assertIn("q5", selected_ids)

    @unittest.skipUnless(pd is not None, "pandas is required for sampling tests")
    def test_challenge_sampling_rejects_missing_or_overflow(self):
        frame = pd.DataFrame({"qid": [f"q{i}" for i in range(6)], "answer": ["x"] * 6})
        with self.assertRaisesRegex(ValueError, "missing from train subset"):
            select_samples(frame, "qid", 4, sample_seed=7,
                            challenge_ids=["missing"])
        with self.assertRaisesRegex(ValueError, "exceed num_samples"):
            select_samples(frame, "qid", 3, sample_seed=7,
                            anchor_ids=["q0", "q1", "q2", "q3"],
                            challenge_ids=["q2", "q3", "q4"])


if __name__ == "__main__":
    unittest.main()
