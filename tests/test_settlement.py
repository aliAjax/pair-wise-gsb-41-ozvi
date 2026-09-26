import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402
from settlement_calc import item_payout, split_settlement  # noqa: E402


class SettlementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def claim_in_review(self):
        claim = self.service.create_claim(
            "intake1", "intake", "S-001", "TY-2026", "A区", "flood", "P-1", "R-1",
            30.1, 121.1, 1000000, False, False,
        )
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1, False)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.5, "结构受损", "赔付", claim["version"])
        return self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])

    def add_items(self, claim_id):
        self.service.upsert_loss_item("adjuster1", "adjuster", claim_id, "厂房", 1000000, 0.5, 20000, 30000)
        self.service.upsert_loss_item("adjuster1", "adjuster", claim_id, "设备", 500000, 0.4, 0, 10000)

    def review_all(self, claim_id):
        view = self.service.settlement_view("sup1", "supervisor", claim_id)
        for item in view["items"]:
            self.service.review_loss_item("sup1", "supervisor", item["id"])
        return self.service.settlement_view("sup1", "supervisor", claim_id)

    def test_itemized_settlement_full_flow(self):
        claim = self.claim_in_review()
        self.add_items(claim["id"])
        view = self.review_all(claim["id"])
        view = self.service.set_coinsurers("sup1", "supervisor", claim["id"], [
            {"name": "人保", "share_pct": 60}, {"name": "平安", "share_pct": 40},
        ])
        self.assertTrue(view["ready"])
        self.assertEqual([], view["issues"])
        # 厂房 1000000*0.5-20000-30000=450000；设备 500000*0.4-0-10000=190000
        self.assertEqual(450000, item_payout({"sum_insured": 1000000, "loss_ratio": 0.5, "salvage": 20000, "deductible": 30000}))
        self.assertEqual(640000, view["preview"]["total_payout"])
        view = self.service.approve_settlement("sup1", "supervisor", claim["id"], view["claim"]["version"], "首次核定")
        self.assertEqual("approved", view["claim"]["status"])
        self.assertEqual(640000, view["claim"]["final_payout"])
        self.assertEqual(1, len(view["versions"]))
        lines = view["versions"][0]["lines"]
        self.assertEqual(4, len(lines))
        for name, payout in (("厂房", 450000), ("设备", 190000)):
            self.assertEqual(payout, round(sum(l["amount"] for l in lines if l["item_name"] == name), 2))
        self.assertEqual(270000, sum(l["amount"] for l in lines if l["coinsurer"] == "人保" and l["item_name"] == "厂房"))
        # 核定后冻结：标的与共保份额都不可再改
        with self.assertRaises(DomainError) as ctx:
            self.service.upsert_loss_item("adjuster1", "adjuster", claim["id"], "库存", 100000, 0.1, 0, 0)
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError):
            self.service.set_coinsurers("sup1", "supervisor", claim["id"], [{"name": "人保", "share_pct": 100}])

    def test_approval_blocked_with_specific_issues(self):
        claim = self.claim_in_review()
        # 厂房：100000*0.2-5000-30000 = -15000（负赔款），且两项都未复核，份额只有 70%
        self.service.upsert_loss_item("adjuster1", "adjuster", claim["id"], "厂房", 100000, 0.2, 5000, 30000)
        self.service.upsert_loss_item("adjuster1", "adjuster", claim["id"], "设备", 500000, 0.4, 0, 10000)
        self.service.set_coinsurers("sup1", "supervisor", claim["id"], [{"name": "人保", "share_pct": 70}])
        view = self.service.settlement_view("sup1", "supervisor", claim["id"])
        self.assertFalse(view["ready"])
        codes = [i["code"] for i in view["issues"]]
        self.assertIn("item_unreviewed", codes)
        self.assertIn("item_negative_payout", codes)
        self.assertIn("shares_below_100", codes)
        named = {i["item"] for i in view["issues"] if i["item"]}
        self.assertIn("厂房", named)  # 页面可指出具体缺少/异常的标的
        self.assertIn("设备", named)
        with self.assertRaises(DomainError) as ctx:
            self.service.approve_settlement("sup1", "supervisor", claim["id"], view["claim"]["version"])
        self.assertEqual(409, ctx.exception.status)
        self.assertTrue(ctx.exception.extra["issues"])

    def test_supplement_creates_new_version_and_keeps_old(self):
        claim = self.claim_in_review()
        self.add_items(claim["id"])
        view = self.review_all(claim["id"])
        self.service.set_coinsurers("sup1", "supervisor", claim["id"], [{"name": "人保", "share_pct": 100}])
        view = self.service.approve_settlement("sup1", "supervisor", claim["id"], view["claim"]["version"], "首次核定")
        self.assertEqual(640000, view["claim"]["final_payout"])
        # 补证重开：状态回到 review，可再次维护标的
        view = self.service.supplement_settlement("adjuster1", "adjuster", claim["id"], view["claim"]["version"], "补充设备发票")
        self.assertEqual("review", view["claim"]["status"])
        view = self.service.upsert_loss_item("adjuster1", "adjuster", claim["id"], "设备", 500000, 0.5, 0, 10000)
        item = next(i for i in view["items"] if i["name"] == "设备")
        self.assertFalse(bool(item["reviewed"]))  # 更新后需重新复核
        self.service.review_loss_item("sup1", "supervisor", item["id"])
        view = self.service.settlement_view("sup1", "supervisor", claim["id"])
        view = self.service.approve_settlement("sup1", "supervisor", claim["id"], view["claim"]["version"], "补证后重新核定")
        # 设备 500000*0.5-10000=240000，总赔款 450000+240000=690000
        self.assertEqual(690000, view["claim"]["final_payout"])
        self.assertEqual(2, len(view["versions"]))
        v1 = next(v for v in view["versions"] if v["version_no"] == 1)
        v2 = next(v for v in view["versions"] if v["version_no"] == 2)
        self.assertEqual(640000, v1["total_payout"])  # 旧结算仍可查且未被改写
        self.assertEqual(190000, sum(l["amount"] for l in v1["lines"] if l["item_name"] == "设备"))
        self.assertEqual(690000, v2["total_payout"])

    def test_permissions_and_guards(self):
        claim = self.claim_in_review()
        self.add_items(claim["id"])
        view = self.service.settlement_view("sup1", "supervisor", claim["id"])
        own = view["items"][0]
        with self.assertRaises(DomainError) as ctx:  # 录入人不能复核自己的标的
            self.service.review_loss_item("adjuster1", "adjuster", own["id"])
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:  # 非主管不能维护份额
            self.service.set_coinsurers("adjuster1", "adjuster", claim["id"], [{"name": "人保", "share_pct": 100}])
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:  # 非主管不能核定
            self.service.approve_settlement("adjuster1", "adjuster", claim["id"], view["claim"]["version"])
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:  # 已维护标的后不能再走手工总额核定
            self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 100000, view["claim"]["version"])
        self.assertEqual(409, ctx.exception.status)

    def test_split_rounding_remainder_goes_to_last_coinsurer(self):
        items = [{"name": "X", "sum_insured": 100.01, "loss_ratio": 1, "salvage": 0, "deductible": 0}]
        coins = [{"name": "A", "share_pct": 33.33}, {"name": "B", "share_pct": 33.33}, {"name": "C", "share_pct": 33.34}]
        result = split_settlement(items, coins)
        self.assertEqual(100.01, result["total_payout"])
        self.assertEqual(100.01, round(sum(l["amount"] for l in result["lines"]), 2))


if __name__ == "__main__":
    unittest.main()
