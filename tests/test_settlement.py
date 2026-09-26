import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402
from settlement_calc import CalcError, approval_blockers, split_settlement  # noqa: E402
from settlement_store import StoreError  # noqa: E402

SHARES = [
    {"insurer": "A保险", "share_pct": 50},
    {"insurer": "B保险", "share_pct": 30},
    {"insurer": "C保险", "share_pct": 20},
]


class SettlementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def claim_in_review(self):
        claim = self.service.create_claim(
            "intake1", "intake", "C-100", "TY-2026", "A区", "flood", "P-100",
            "R-100", 30.1, 121.1, 500000, False, True)
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1, False)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.6, "结构受损", "部分赔付", claim["version"])
        return self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])

    def draft_with_items(self, review=True, shares=None):
        claim = self.claim_in_review()
        cid = claim["id"]
        self.service.create_settlement_version("sup1", "supervisor", cid, "首次定损")
        i1 = self.service.add_settlement_item("adjuster1", "adjuster", cid, "厂房主体", 400000, 0.6, 20000, 50000)
        i2 = self.service.add_settlement_item("adjuster1", "adjuster", cid, "库存物资", 150000, 0.8, 5000, 10000)
        if review:
            self.service.review_settlement_item("adjuster1", "adjuster", cid, i1["id"], "reviewed")
            self.service.review_settlement_item("adjuster1", "adjuster", cid, i2["id"], "reviewed")
        if shares is not None:
            self.service.set_coinsurance_shares("sup1", "supervisor", cid, shares)
        return cid, i1, i2

    # ---- 计算层 ----

    def test_split_rounding_drift_goes_to_largest_shareholder(self):
        items = [{"item_name": "X", "insured_amount": 100001, "loss_ratio": 0.3333,
                  "salvage_value": 0, "deductible": 0, "review_status": "reviewed"}]
        shares = [{"insurer": "A", "share_pct": 33.33}, {"insurer": "B", "share_pct": 33.33},
                  {"insurer": "C", "share_pct": 33.34}]
        result = split_settlement(items, shares)
        self.assertEqual(round(items[0]["insured_amount"] * 0.3333, 2), result["total_payout"])
        self.assertEqual(result["total_payout"], round(sum(l["amount"] for l in result["lines"]), 2))
        top = [l for l in result["lines"] if l["insurer"] == "C"][0]
        self.assertEqual(top["amount"], max(l["amount"] for l in result["lines"]))

    def test_blockers_listed_specifically(self):
        items = [{"item_name": "屋顶", "insured_amount": 100000, "loss_ratio": 0.5,
                  "salvage_value": 0, "deductible": 0, "review_status": "draft"}]
        blockers = approval_blockers(items, [{"insurer": "A", "share_pct": 60}])
        self.assertTrue(any("屋顶" in b and "尚未复核" in b for b in blockers))
        self.assertTrue(any("60.00%" in b and "缺口 40.00%" in b for b in blockers))
        self.assertEqual([], approval_blockers(
            [{**items[0], "review_status": "reviewed"}], [{"insurer": "A", "share_pct": 100}]))

    def test_negative_payout_blocked_then_fixed_by_removal(self):
        cid, _, _ = self.draft_with_items(review=True, shares=SHARES)
        bad = self.service.add_settlement_item("adjuster1", "adjuster", cid, "机器", 100000, 0.5, 30000, 40000)
        self.assertLess(bad["payout"], 0)  # 残值+免赔额超过损失额，赔款为负
        blockers = self.service.get_settlement("sup1", "supervisor", cid)["blockers"]
        self.assertTrue(any("机器" in b and "赔款为负" in b for b in blockers))
        self.assertTrue(any("机器" in b and "尚未复核" in b for b in blockers))
        with self.assertRaises(DomainError) as ctx:
            self.service.finalize_settlement("sup1", "supervisor", cid)
        self.assertIn("机器", str(ctx.exception))
        # 删除录错项后恢复可核定
        self.service.remove_settlement_item("adjuster1", "adjuster", cid, bad["id"])
        result = self.service.finalize_settlement("sup1", "supervisor", cid)
        self.assertEqual("finalized", result["version"]["status"])

    # ---- 服务层：完整流程 ----

    def test_full_settlement_flow_and_split(self):
        cid, _, _ = self.draft_with_items(shares=SHARES)
        result = self.service.finalize_settlement("sup1", "supervisor", cid)
        self.assertEqual("finalized", result["version"]["status"])
        self.assertEqual(275000.0, result["version"]["total_payout"])
        self.assertEqual(6, len(result["lines"]))
        by_insurer = {}
        for line in result["lines"]:
            by_insurer[line["insurer"]] = by_insurer.get(line["insurer"], 0) + line["amount"]
        self.assertEqual({"A保险": 137500.0, "B保险": 82500.0, "C保险": 55000.0}, by_insurer)
        claim = self.service.get_settlement("sup1", "supervisor", cid)["claim"]
        self.assertEqual(275000.0, claim["final_payout"])

    def test_finalize_blocked_until_all_conditions_met(self):
        cid, i1, i2 = self.draft_with_items(review=False, shares=[{"insurer": "A保险", "share_pct": 60}])
        with self.assertRaises(DomainError) as ctx:
            self.service.finalize_settlement("sup1", "supervisor", cid)
        msg = str(ctx.exception)
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("厂房主体", msg)          # 未复核项被点名
        self.assertIn("库存物资", msg)
        self.assertIn("60.00%", msg)           # 份额不足被点名
        # 全部复核后仍因份额不足被挡，页面可拿到具体清单
        self.service.review_settlement_item("adjuster1", "adjuster", cid, i1["id"], "reviewed")
        self.service.review_settlement_item("adjuster1", "adjuster", cid, i2["id"], "reviewed")
        detail = self.service.get_settlement("sup1", "supervisor", cid)
        self.assertEqual(1, len(detail["blockers"]))
        self.assertIn("60.00%", detail["blockers"][0])
        self.service.set_coinsurance_shares("sup1", "supervisor", cid, SHARES)
        result = self.service.finalize_settlement("sup1", "supervisor", cid)
        self.assertEqual("finalized", result["version"]["status"])

    def test_frozen_version_immutable_and_supplement_creates_new_version(self):
        cid, i1, _ = self.draft_with_items(shares=SHARES)
        v1 = self.service.finalize_settlement("sup1", "supervisor", cid)
        # 冻结后不能改旧版
        with self.assertRaises(StoreError) as ctx:
            self.service.add_settlement_item("adjuster1", "adjuster", cid, "新损失", 10000, 0.5)
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(StoreError):
            self.service.review_settlement_item("adjuster1", "adjuster", cid, i1["id"], "draft")
        with self.assertRaises(StoreError):
            self.service.set_coinsurance_shares("sup1", "supervisor", cid, SHARES)
        # 补证：新建版本，旧版结算仍可查
        self.service.create_settlement_version("sup1", "supervisor", cid, "补证：新增设备损失")
        self.service.add_settlement_item("adjuster1", "adjuster", cid, "生产设备", 200000, 0.3, 10000, 20000)
        versions = self.service.list_settlement_versions("sup1", "supervisor", cid)["versions"]
        self.assertEqual([2, 1], [v["version_no"] for v in versions])
        old = self.service.get_settlement("auditor1", "auditor", cid, version_no=1)
        self.assertEqual("finalized", old["settlement"]["version"]["status"])
        self.assertEqual(275000.0, old["settlement"]["version"]["total_payout"])
        self.assertEqual(6, len(old["settlement"]["lines"]))
        self.assertIsNone(old["blockers"])
        # 新版草稿独立维护，不影响旧版
        draft = self.service.get_settlement("sup1", "supervisor", cid)
        self.assertEqual(2, draft["settlement"]["version"]["version_no"])
        self.assertTrue(any("尚未复核" in b for b in draft["blockers"]))
        self.assertTrue(any("共保份额" in b for b in draft["blockers"]))
        self.assertEqual(275000.0, v1["version"]["total_payout"])

    def test_second_version_finalize_keeps_both_queryable(self):
        cid, _, _ = self.draft_with_items(shares=SHARES)
        self.service.finalize_settlement("sup1", "supervisor", cid)
        self.service.create_settlement_version("sup1", "supervisor", cid, "补证")
        item = self.service.add_settlement_item("adjuster1", "adjuster", cid, "生产设备", 200000, 0.3, 10000, 20000)
        self.service.review_settlement_item("adjuster1", "adjuster", cid, item["id"], "reviewed")
        self.service.set_coinsurance_shares("sup1", "supervisor", cid, [{"insurer": "A保险", "share_pct": 100}])
        v2 = self.service.finalize_settlement("sup1", "supervisor", cid)
        self.assertEqual(30000.0, v2["version"]["total_payout"])
        self.assertEqual(1, len(v2["lines"]))
        state = self.service.state("sup1", "supervisor")
        self.assertEqual(2, state["settlements"][0]["version_no"])
        old = self.service.get_settlement("sup1", "supervisor", cid, version_no=1)
        self.assertEqual(275000.0, old["settlement"]["version"]["total_payout"])

    def test_permissions_and_stage_gating(self):
        claim = self.service.create_claim(
            "intake1", "intake", "C-200", "TY-2026", "A区", "flood", "P-200",
            "R-200", 30.3, 121.2, 100000, False, False)
        with self.assertRaises(DomainError) as ctx:  # 未到复核环节
            self.service.create_settlement_version("sup1", "supervisor", claim["id"])
        self.assertEqual(409, ctx.exception.status)
        cid, _, _ = self.draft_with_items(shares=SHARES)
        with self.assertRaises(DomainError) as ctx2:  # 查勘员无权建版本
            self.service.create_settlement_version("s1", "surveyor", cid)
        self.assertEqual(403, ctx2.exception.status)
        with self.assertRaises(DomainError) as ctx3:  # 定损员无权维护份额
            self.service.set_coinsurance_shares("adjuster1", "adjuster", cid, SHARES)
        self.assertEqual(403, ctx3.exception.status)
        with self.assertRaises(DomainError) as ctx4:  # 非主管不能核定
            self.service.finalize_settlement("adjuster1", "adjuster", cid)
        self.assertEqual(403, ctx4.exception.status)
        with self.assertRaises(DomainError) as ctx5:  # 外部角色不能查看结算
            self.service.get_settlement("x", "viewer", cid)
        self.assertEqual(403, ctx5.exception.status)

    def test_share_validation(self):
        cid, _, _ = self.draft_with_items()
        with self.assertRaises(CalcError):
            self.service.set_coinsurance_shares("sup1", "supervisor", cid,
                                                [{"insurer": "A", "share_pct": 50}, {"insurer": "A", "share_pct": 50}])
        with self.assertRaises(CalcError):
            self.service.set_coinsurance_shares("sup1", "supervisor", cid, [{"insurer": "A", "share_pct": 0}])
        with self.assertRaises(CalcError):
            self.service.set_coinsurance_shares("sup1", "supervisor", cid, [])


if __name__ == "__main__":
    unittest.main()
