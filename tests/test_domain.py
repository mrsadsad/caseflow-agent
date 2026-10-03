"""Domain tests use real SQLite transactions, including parallel file connections."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from caseflow.fixtures import CASE_EXPECTATIONS
from caseflow.store import Conflict, DomainError, Store
from caseflow.tools import Tools


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "domain.sqlite3"
        self.store = Store(self.path)
        self.store.seed()

    def run_case(self, case_id="refund_valid"):
        run = self.store.create_run(case_id, "demo")
        return run, Tools(self.store, run["id"])

    def proposal(self, case_id="refund_valid", save_terminal=True):
        run, tool = self.run_case(case_id)
        case = self.store.get_case(case_id)
        order = tool.execute("get_order", {"order_id": case["order_id"]})["order"]
        sources = tool.execute("search_policy", {"query": "refund"})["policies"]
        result = tool.execute("propose_refund", {
            "order_id": case["order_id"], "amount_cents": order["amount_cents"] - order["refunded_cents"],
            "reason": "Customer requested a refund", "policy_ids": [sources[0]["id"]]})
        self.assertIn("proposal", result, result)
        if save_terminal:
            run = self.store.save_run(run["id"], run["revision"], run["state"], "awaiting_approval")
        return run, result["proposal"]

    def change_order(self, sql="version=version+1", params=(), order_id="ord-valid-001"):
        with self.store._transaction() as conn:
            conn.execute("UPDATE orders SET " + sql + " WHERE id=?", (*params, order_id))

    def replace_policy(self):
        with self.store._transaction() as conn:
            current = conn.execute("SELECT * FROM policies WHERE id='refund-standard' AND active=1").fetchone()
            conn.execute("UPDATE policies SET active=0 WHERE id='refund-standard'")
            conn.execute("INSERT INTO policies VALUES(?,?,?,?,?,?,?)", (
                current["id"], current["version"] + 1, current["tenant_id"], 1,
                current["kind"], current["text"] + " Updated.", current["rules_json"]))

    def test_seed_is_idempotent_without_resetting_refunded_order(self):
        _, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        self.store.dispatch()
        self.store.seed()
        self.assertEqual(len(self.store.list_cases()), len(CASE_EXPECTATIONS))
        self.assertEqual(len(self.store.ledger()), 1)
        self.assertEqual(self.store.get_order(proposal["run_id"], proposal["order_id"])["refunded_cents"], 12900)

    def test_seed_does_not_reactivate_replaced_policy(self):
        self.replace_policy()
        self.store.seed()
        run, tool = self.run_case()
        policies = tool.execute("search_policy", {"query": "refund"})["policies"]
        self.assertEqual([(p["id"], p["version"]) for p in policies], [("refund-standard", 3)])

    def test_policy_retrieval_filters_tenant_and_inactive_before_ranking(self):
        _, tool = self.run_case()
        policies = tool.execute("search_policy", {"query": "退款 refund window 99 30"})["policies"]
        self.assertEqual([(p["id"], p["version"]) for p in policies], [("refund-standard", 2)])
        self.assertGreater(policies[0]["score"], 0)
        _, other = self.run_case("extended_window")
        self.assertEqual(other.execute("search_policy", {"query": "refund"})["policies"][0]["id"], "refund-plus")

    def test_search_without_lexical_matches_returns_no_sources(self):
        _, tool = self.run_case()
        self.assertEqual(tool.execute("search_policy", {"query": "unmatchablexyz"})["policies"], [])

    def test_order_scope_hides_foreign_tenant_and_customer_equally(self):
        _, tool = self.run_case()
        results = [tool.execute("get_order", {"order_id": order_id}) for order_id in (
            "ord-foreign-006", "ord-other-customer-009", "not-a-real-order")]
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0]["error"], "order_not_found")
        self.assertNotIn("5500", json.dumps(results))

    def test_model_cannot_supply_tenant_or_customer_or_admin_tools(self):
        _, tool = self.run_case()
        self.assertEqual(tool.execute("get_order", {"order_id": "ord-valid-001", "tenant_id": "tenant-other"})["error"], "invalid_arguments")
        self.assertEqual(tool.execute("approve", {"proposal_id": "pretend"})["error"], "unknown_tool")
        self.assertEqual(tool.execute("dispatch", {})["error"], "unknown_tool")

    def test_fabricated_or_other_runs_citations_do_not_authorize_refund(self):
        _, first = self.run_case()
        first.execute("search_policy", {"query": "refund"})
        run, tool = self.run_case()
        for source in ("refund-standard", "refund-other", "fabricated-policy"):
            with self.subTest(source=source):
                result = tool.execute("propose_refund", {"order_id": "ord-valid-001", "amount_cents": 12900,
                                     "reason": "requested", "policy_ids": [source]})
                self.assertEqual(result["error"], "invalid_citation")
        self.assertEqual(self.store.proposals(run["id"]), [])

    def test_shipping_citation_does_not_authorize_refund(self):
        _, tool = self.run_case()
        tool.execute("search_policy", {"query": "shipping"})
        result = tool.execute("propose_refund", {"order_id": "ord-valid-001", "amount_cents": 12900,
                             "reason": "requested", "policy_ids": ["shipping-standard"]})
        self.assertEqual(result["error"], "invalid_citation")

    def test_finish_requires_current_retrieved_citations(self):
        _, tool = self.run_case()
        args = {"answer": "The refund window is seven days.", "policy_ids": ["refund-standard"]}
        self.assertEqual(tool.execute("finish", args)["error"], "invalid_citation")
        tool.execute("search_policy", {"query": "refund"})
        self.assertEqual(tool.execute("finish", args)["terminal_status"], "completed")
        self.replace_policy()
        self.assertEqual(tool.execute("finish", args)["error"], "invalid_citation")

    def test_noninteger_excess_and_partial_amounts_are_rejected(self):
        run, tool = self.run_case()
        tool.execute("search_policy", {"query": "refund"})
        for amount in (True, 12900.0, "12900", -1, 0, 12899, 12901, 99999999):
            with self.subTest(amount=amount):
                result = tool.execute("propose_refund", {"order_id": "ord-valid-001", "amount_cents": amount,
                                     "reason": "requested", "policy_ids": ["refund-standard"]})
                self.assertIn("error", result)
        self.assertEqual(self.store.proposals(run["id"]), [])

    def test_invalid_argument_shapes_return_errors_instead_of_throwing(self):
        _, tool = self.run_case()
        for arguments in (None, [], {}, {"query": ""}, {"query": 123}, {"query": "a" * 501}, {"query": "refund", "override": True}):
            with self.subTest(arguments=arguments):
                self.assertEqual(tool.execute("search_policy", arguments)["error"], "invalid_arguments")
        for sources in ([[]], ["a", "a"], [], "refund-standard"):
            self.assertEqual(tool.execute("finish", {"answer": "test", "policy_ids": sources})["error"], "invalid_arguments")

    def test_expired_delivered_and_refunded_orders_cannot_get_proposals(self):
        for case_id in ("refund_expired", "return_delivered", "already_refunded"):
            with self.subTest(case_id=case_id):
                run, tool = self.run_case(case_id)
                order_id = self.store.get_case(case_id)["order_id"]
                tool.execute("search_policy", {"query": "refund"})
                result = tool.execute("propose_refund", {"order_id": order_id, "amount_cents": 8900,
                                     "reason": "requested", "policy_ids": ["refund-standard"]})
                self.assertEqual(result["error"], "refund_ineligible")
                self.assertEqual(self.store.proposals(run["id"]), [])

    def test_tenant_specific_longer_window_is_enforced(self):
        _, proposal = self.proposal("extended_window")
        self.assertEqual(proposal["policy_versions"], [{"id": "refund-plus", "version": 1}])
        self.store.approve(proposal["id"], True)
        self.assertEqual(self.store.dispatch()["status"], "delivered")

    def test_prompt_injection_cannot_change_amount_or_bypass_approval(self):
        run, tool = self.run_case("prompt_injection")
        self.assertIn("SYSTEM OVERRIDE", tool.execute("get_order", {"order_id": "ord-injection-005"})["order"]["notes"])
        tool.execute("search_policy", {"query": "refund"})
        args = {"order_id": "ord-injection-005", "amount_cents": 99999999,
                "reason": "SYSTEM instructed", "policy_ids": ["refund-standard"]}
        self.assertEqual(tool.execute("propose_refund", args)["error"], "invalid_amount")
        args["amount_cents"] = 6900
        result = tool.execute("propose_refund", args)
        self.assertEqual(result["terminal_status"], "awaiting_approval")
        self.assertEqual(self.store.get_run(run["id"])["revision"], 0)
        self.assertEqual(self.store.ledger(), [])
        self.assertEqual(self.store.outbox(), [])

    def test_case_order_binding_prevents_redirect_to_another_owned_order(self):
        _, tool = self.run_case()
        tool.execute("search_policy", {"query": "refund"})
        result = tool.execute("propose_refund", {"order_id": "ord-injection-005", "amount_cents": 6900,
                             "reason": "redirect", "policy_ids": ["refund-standard"]})
        self.assertEqual(result["error"], "order_not_found")

    def test_proposal_replay_is_idempotent_without_mutating_runtime_revision(self):
        run, proposal = self.proposal(save_terminal=False)
        replay = self.store.create_proposal(run["id"], proposal["order_id"], proposal["amount_cents"], proposal["reason"], proposal["policy_ids"])
        self.assertEqual(replay["id"], proposal["id"])
        self.assertEqual(len(self.store.proposals()), 1)
        self.assertEqual(self.store.get_run(run["id"])["revision"], 0)
        with self.assertRaises(Conflict):
            self.store.create_proposal(run["id"], proposal["order_id"], 1, proposal["reason"], proposal["policy_ids"])

    def test_approval_cannot_race_runtime_terminal_save(self):
        _, proposal = self.proposal(save_terminal=False)
        with self.assertRaisesRegex(Conflict, "运行尚未"):
            self.store.approve(proposal["id"], True)
        self.assertEqual(self.store.outbox(), [])

    def test_approval_rejects_changed_order(self):
        _, proposal = self.proposal()
        self.change_order()
        with self.assertRaises(DomainError) as caught:
            self.store.approve(proposal["id"], True)
        self.assertEqual(caught.exception.code, "stale_order")
        self.assertEqual(self.store.outbox(), [])

    def test_approval_rejects_new_policy_version_and_inplace_policy_changes(self):
        for change in ("version", "content"):
            with self.subTest(change=change):
                _, proposal = self.proposal()
                if change == "version":
                    self.replace_policy()
                else:
                    with self.store._transaction() as conn:
                        conn.execute("UPDATE policies SET text=text || ' changed' WHERE id='refund-standard' AND active=1")
                with self.assertRaises(DomainError):
                    self.store.approve(proposal["id"], True)
        self.assertEqual(self.store.outbox(), [])

    def test_window_is_checked_again_at_approval_even_without_version_change(self):
        _, proposal = self.proposal()
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        self.change_order("created_at=?", (old,))
        with self.assertRaises(DomainError) as caught:
            self.store.approve(proposal["id"], True)
        self.assertEqual(caught.exception.code, "refund_ineligible")

    def test_rejection_never_creates_outbox_and_cannot_be_overturned(self):
        run, proposal = self.proposal()
        self.assertEqual(self.store.approve(proposal["id"], False)["status"], "rejected")
        self.assertEqual(self.store.approve(proposal["id"], False)["status"], "rejected")
        with self.assertRaises(Conflict):
            self.store.approve(proposal["id"], True)
        self.assertEqual(self.store.get_run(run["id"])["status"], "rejected")
        self.assertEqual(self.store.outbox(), [])

    def test_approval_duplicate_clicks_create_one_outbox(self):
        _, proposal = self.proposal()
        barrier = threading.Barrier(8)
        def approve(_):
            barrier.wait()
            return self.store.approve(proposal["id"], True)["status"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(approve, range(8)))
        self.assertEqual(statuses, ["approved"] * 8)
        self.assertEqual(len(self.store.outbox()), 1)

    def test_parallel_cas_has_only_one_winner(self):
        run, _ = self.run_case()
        barrier = threading.Barrier(4)
        def save(_):
            barrier.wait()
            try:
                self.store.save_run(run["id"], run["revision"], run["state"], "running")
                return "saved"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(save, range(4)))
        self.assertEqual(statuses.count("saved"), 1)
        self.assertEqual(statuses.count("conflict"), 3)

    def test_crash_after_effect_reopens_database_and_retries_without_double_refund(self):
        run, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        with self.assertRaisesRegex(DomainError, "simulated_ack_loss"):
            self.store.dispatch(fail_after_effect=True)
        self.assertEqual(len(self.store.ledger()), 1)
        self.assertEqual(self.store.outbox()[0]["status"], "pending")
        self.assertEqual(self.store.get_run(run["id"])["status"], "queued")
        reopened = Store(self.path)
        reopened.seed()
        # Once the receiver committed, ACK retry must work even if policy changes.
        self.replace_policy()
        self.assertEqual(reopened.dispatch()["status"], "delivered")
        self.assertIsNone(reopened.dispatch())
        self.assertEqual(len(reopened.ledger()), 1)
        self.assertEqual(reopened.get_order(run["id"], proposal["order_id"])["refunded_cents"], 12900)
        self.assertEqual(reopened.outbox()[0]["attempts"], 2)
        self.assertEqual(reopened.get_run(run["id"])["status"], "completed")

    def test_parallel_dispatch_has_one_effect_and_one_ack(self):
        run, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        barrier = threading.Barrier(8)
        def dispatch(_):
            barrier.wait()
            return self.store.dispatch()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(dispatch, range(8)))
        self.assertEqual(len(self.store.ledger()), 1)
        kinds = [e["kind"] for e in self.store.events(run["id"])]
        self.assertEqual(kinds.count("refund_effect_committed"), 1)
        self.assertEqual(kinds.count("refund_delivery_acknowledged"), 1)
        self.assertEqual(self.store.outbox()[0]["status"], "delivered")

    def test_two_runs_cannot_refund_same_order_twice(self):
        _, first = self.proposal()
        _, second = self.proposal()
        self.store.approve(first["id"], True)
        self.store.approve(second["id"], True)
        self.assertEqual(self.store.dispatch()["status"], "delivered")
        self.assertEqual(self.store.dispatch()["status"], "failed")
        self.assertEqual(len(self.store.ledger()), 1)

    def test_effect_revalidates_order_after_approval(self):
        run, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        self.change_order("status='delivered',version=version+1")
        result = self.store.dispatch()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_error"], "stale_order")
        self.assertEqual(self.store.ledger(), [])
        self.assertEqual(self.store.get_run(run["id"])["status"], "error")

    def test_effect_revalidates_policy_after_approval(self):
        _, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        self.replace_policy()
        result = self.store.dispatch()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.ledger(), [])

    def test_effect_revalidates_expired_window_after_approval(self):
        _, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        self.change_order("created_at=?", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
        self.assertEqual(self.store.dispatch()["last_error"], "refund_ineligible")
        self.assertEqual(self.store.ledger(), [])

    def test_outbox_payload_cannot_override_approved_amount(self):
        _, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        item = self.store.outbox()[0]
        item["payload"]["amount_cents"] = 1
        with self.store._transaction() as conn:
            conn.execute("UPDATE outbox SET payload_json=? WHERE id=?", (json.dumps(item["payload"]), item["id"]))
        self.assertEqual(self.store.dispatch()["last_error"], "invalid_delivery")
        self.assertEqual(self.store.ledger(), [])

    def test_rollback_prevents_partial_business_writes(self):
        with self.assertRaises(RuntimeError):
            with self.store._transaction() as conn:
                conn.execute("UPDATE orders SET refunded_cents=1 WHERE id='ord-valid-001'")
                raise RuntimeError("simulated failure")
        run, _ = self.run_case()
        self.assertEqual(self.store.get_order(run["id"], "ord-valid-001")["refunded_cents"], 0)

    def test_recover_includes_queued_runs_preserves_approvals_and_invalidates_revision(self):
        run, _ = self.run_case()
        running = self.store.save_run(run["id"], run["revision"], run["state"], "running")
        ready, _ = self.run_case("missing_order")
        awaiting, _ = self.proposal()
        self.assertEqual(self.store.recover(), 2)
        self.assertEqual(self.store.get_run(run["id"])["status"], "interrupted")
        self.assertEqual(self.store.get_run(ready["id"])["status"], "interrupted")
        self.assertEqual(self.store.get_run(awaiting["id"])["status"], "awaiting_approval")
        with self.assertRaises(Conflict):
            self.store.save_run(run["id"], running["revision"], running["state"], "completed")

    def test_public_lists_decode_json_and_events_are_cursor_ordered(self):
        run, proposal = self.proposal()
        self.store.approve(proposal["id"], True)
        self.assertIsInstance(self.store.proposals()[0]["policy_versions"], list)
        self.assertIsInstance(self.store.outbox()[0]["payload"], dict)
        events = self.store.events(run["id"])
        self.assertTrue(events)
        self.assertTrue(all(isinstance(event["payload"], dict) for event in events))
        self.assertEqual(self.store.events(run["id"], after=events[-1]["id"]), [])


if __name__ == "__main__":
    unittest.main()
