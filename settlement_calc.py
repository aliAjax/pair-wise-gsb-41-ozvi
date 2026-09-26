"""逐项定损与共保结算的纯计算层。

不依赖数据库与 HTTP：输入普通 dict，输出计算结果或阻断清单，
由存储层（settlement_store）和页面（static/settlement.html）分别调用。
"""
from __future__ import annotations

from typing import Any

SHARE_TOLERANCE = 1e-6
VALID_ITEM_STATUS = {"draft", "reviewed"}


class CalcError(Exception):
    """输入数据本身无效（区别于核定阻断，阻断属于业务结果而非异常）。"""


def _num(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CalcError("%s必须是数值" % label) from exc


def validate_item_fields(item: dict[str, Any]) -> dict[str, float | str]:
    """校验单个定损项字段，返回规范化数值；非法输入抛 CalcError。"""
    name = str(item.get("item_name", "")).strip()
    if not name:
        raise CalcError("受损标的名称不能为空")
    insured = _num(item.get("insured_amount"), "保额")
    ratio = _num(item.get("loss_ratio"), "损失比例")
    salvage = _num(item.get("salvage_value", 0), "残值")
    deductible = _num(item.get("deductible", 0), "免赔额")
    if insured <= 0:
        raise CalcError("保额必须大于0")
    if not 0 <= ratio <= 1:
        raise CalcError("损失比例应在 0 到 1 之间")
    if salvage < 0 or deductible < 0:
        raise CalcError("残值和免赔额不能为负数")
    status = str(item.get("review_status", "draft"))
    if status not in VALID_ITEM_STATUS:
        raise CalcError("定损项复核状态无效")
    return {
        "item_name": name,
        "insured_amount": insured,
        "loss_ratio": ratio,
        "salvage_value": salvage,
        "deductible": deductible,
        "review_status": status,
    }


def item_payout(item: dict[str, Any]) -> float:
    """单项赔款 = 保额 × 损失比例 − 残值 − 免赔额。

    结果为负说明残值/免赔额录入有误，会作为核定阻断项被点名，
    只有全部非负后 split_settlement 才会被调用。
    """
    return round(item["insured_amount"] * item["loss_ratio"] - item["salvage_value"] - item["deductible"], 2)


def validate_shares(shares: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    """校验共保份额，返回规范化列表；非法输入抛 CalcError。"""
    if not shares:
        raise CalcError("共保份额不能为空")
    seen: set[str] = set()
    result = []
    for share in shares:
        insurer = str(share.get("insurer", "")).strip()
        if not insurer:
            raise CalcError("共保人名称不能为空")
        if insurer in seen:
            raise CalcError("共保人重复：%s" % insurer)
        seen.add(insurer)
        pct = _num(share.get("share_pct"), "共保份额")
        if not 0 < pct <= 100:
            raise CalcError("共保份额应在 (0, 100] 之间")
        result.append({"insurer": insurer, "share_pct": pct})
    return result


def split_settlement(items: list[dict[str, Any]],
                     shares: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐项赔款按共保份额拆成结算明细。

    每项赔款按份额比例分摊，单项内按行四舍五入到分，
    尾差归入该单项份额最大的共保人，保证明细合计与赔款合计一致。
    """
    lines: list[dict[str, Any]] = []
    for item in items:
        payout = item_payout(item)
        allocated = 0.0
        amounts = []
        for share in shares:
            amount = round(payout * share["share_pct"] / 100.0, 2)
            amounts.append(amount)
            allocated += amount
        if amounts:
            drift = round(payout - allocated, 2)
            top = max(range(len(shares)), key=lambda i: shares[i]["share_pct"])
            amounts[top] = round(amounts[top] + drift, 2)
        for share, amount in zip(shares, amounts):
            lines.append({
                "item_name": item["item_name"],
                "item_payout": payout,
                "insurer": share["insurer"],
                "share_pct": share["share_pct"],
                "amount": amount,
            })
    return {"lines": lines, "total_payout": round(sum(item_payout(i) for i in items), 2)}


def approval_blockers(items: list[dict[str, Any]] | None,
                      shares: list[dict[str, Any]] | None) -> list[str]:
    """返回阻止主管核定的具体原因清单；空列表表示可以核定。"""
    blockers: list[str] = []
    items = items or []
    shares = shares or []
    if not items:
        blockers.append("缺少定损项：尚未维护任何受损标的")
    else:
        for item in items:
            if item.get("review_status") != "reviewed":
                blockers.append("定损项「%s」尚未复核" % item["item_name"])
            if item_payout(item) < 0:
                blockers.append("定损项「%s」赔款为负（%.2f），请检查残值或免赔额"
                                % (item["item_name"], item_payout(item)))
    if not shares:
        blockers.append("缺少共保份额：尚未维护任何共保人")
    else:
        total = round(sum(s["share_pct"] for s in shares), 6)
        if total < 100.0 - SHARE_TOLERANCE:
            blockers.append("共保份额合计 %.2f%%，不足 100%%（缺口 %.2f%%）" % (total, 100.0 - total))
        elif total > 100.0 + SHARE_TOLERANCE:
            blockers.append("共保份额合计 %.2f%%，超过 100%%" % total)
    return blockers
