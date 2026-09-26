"""逐项定损与共保分摊的纯计算逻辑。

本模块不接触数据库与 HTTP，只接收字典列表并返回计算结果，
业务规则（赔款公式、核定前检查、份额拆分）集中在这里，便于单独测试。
"""
from __future__ import annotations

from typing import Any

SHARE_TOLERANCE = 0.01  # 份额合计允许的尾差（百分点）


def item_payout(item: dict[str, Any]) -> float:
    """单项赔款 = 保额 × 损失比例 − 残值 − 免赔额（保留两位小数）。"""
    raw = (
        float(item["sum_insured"]) * float(item["loss_ratio"])
        - float(item["salvage"])
        - float(item["deductible"])
    )
    return round(raw, 2)


def readiness_issues(items: list[dict[str, Any]], coinsurers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """核定前检查：返回全部阻断问题（含具体标的），空列表表示可以核定。"""
    issues: list[dict[str, Any]] = []
    if not items:
        issues.append({"code": "no_items", "item": None, "message": "尚未维护任何受损标的"})
    for item in items:
        if not item["reviewed"]:
            issues.append({
                "code": "item_unreviewed",
                "item": item["name"],
                "message": "标的「%s」尚未复核" % item["name"],
            })
        if item_payout(item) < 0:
            issues.append({
                "code": "item_negative_payout",
                "item": item["name"],
                "message": "标的「%s」试算赔款为负（残值与免赔额合计高于保额×损失比例）" % item["name"],
            })
    if not coinsurers:
        issues.append({"code": "no_coinsurers", "item": None, "message": "尚未维护共保人份额"})
    else:
        share_total = round(sum(float(c["share_pct"]) for c in coinsurers), 2)
        if share_total < 100 - SHARE_TOLERANCE:
            issues.append({
                "code": "shares_below_100",
                "item": None,
                "message": "共保份额合计不足100%%（当前 %.2f%%，尚缺 %.2f%%）" % (share_total, round(100 - share_total, 2)),
            })
        elif share_total > 100 + SHARE_TOLERANCE:
            issues.append({
                "code": "shares_above_100",
                "item": None,
                "message": "共保份额合计超过100%%（当前 %.2f%%，超出 %.2f%%）" % (share_total, round(share_total - 100, 2)),
            })
    return issues


def split_settlement(items: list[dict[str, Any]], coinsurers: list[dict[str, Any]]) -> dict[str, Any]:
    """把每项赔款按共保份额拆成结算明细；分位尾差由末位共保人承担。"""
    lines: list[dict[str, Any]] = []
    total = 0.0
    for item in items:
        payout = item_payout(item)
        total = round(total + payout, 2)
        amounts = [round(payout * float(c["share_pct"]) / 100.0, 2) for c in coinsurers]
        if amounts:
            amounts[-1] = round(amounts[-1] + round(payout - sum(amounts), 2), 2)
        for coinsurer, amount in zip(coinsurers, amounts):
            lines.append({
                "item_name": item["name"],
                "sum_insured": float(item["sum_insured"]),
                "loss_ratio": float(item["loss_ratio"]),
                "salvage": float(item["salvage"]),
                "deductible": float(item["deductible"]),
                "item_payout": payout,
                "coinsurer": coinsurer["name"],
                "share_pct": float(coinsurer["share_pct"]),
                "amount": amount,
            })
    return {"total_payout": total, "lines": lines}
