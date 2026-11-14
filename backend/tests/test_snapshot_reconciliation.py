"""快照对账纯函数测试：批次新旧判定 + 撤回/下架/重挂分类（不依赖数据库）。"""
from __future__ import annotations

from datetime import date

from src.constants.enums import InvalidReason
from src.services.snapshot_service import is_stale_batch, plan_reconciliation


def test_is_stale_batch_uses_snapshot_date():
    baseline = date(2026, 11, 1)
    # 无已对账批次时，任何批次都不算旧
    assert is_stale_batch(baseline, None) is False
    # 晚到的旧批次
    assert is_stale_batch(date(2026, 10, 31), baseline) is True
    # 同快照日重推不算旧（同一天多文件重推仍走幂等/对账）
    assert is_stale_batch(baseline, baseline) is False
    # 更新的批次正常对账
    assert is_stale_batch(date(2026, 11, 2), baseline) is False


def test_plan_classifies_withdrawn_when_lid_missing_from_new_batch():
    current = [{"lid": "L1"}, {"lid": "L2"}]
    plan = plan_reconciliation(current, batch_lids={"L2"}, batch_status={"L2": "active"})
    assert [item["lid"] for item in plan["withdraw"]] == ["L1"]
    assert plan["withdraw"][0]["reason"] == InvalidReason.WITHDRAWN.value
    assert [item["lid"] for item in plan["keep"]] == ["L2"]
    assert plan["deactivate"] == []


def test_plan_classifies_off_market_and_sold():
    current = [{"lid": "L1"}, {"lid": "L2"}, {"lid": "L3"}]
    plan = plan_reconciliation(
        current,
        batch_lids={"L1", "L2", "L3"},
        batch_status={"L1": "active", "L2": "off_market", "L3": "sold"},
    )
    reasons = {item["lid"]: item["reason"] for item in plan["deactivate"]}
    assert reasons == {"L2": InvalidReason.OFF_MARKET.value, "L3": InvalidReason.SOLD.value}
    assert [item["lid"] for item in plan["keep"]] == ["L1"]
    assert plan["withdraw"] == []


def test_plan_default_status_is_active():
    plan = plan_reconciliation([{"lid": "L1"}], {"L1"}, {})
    assert plan["keep"] == [{"lid": "L1"}]
    assert plan["withdraw"] == plan["deactivate"] == []


def test_plan_is_deterministic_and_disjoint():
    current = [{"lid": f"L{i}"} for i in range(6)]
    batch_lids = {"L1", "L3", "L5"}
    plan = plan_reconciliation(
        current, batch_lids, {"L1": "active", "L3": "off_market", "L5": "sold"}
    )
    acted = (
        [x["lid"] for x in plan["withdraw"]]
        + [x["lid"] for x in plan["deactivate"]]
        + [x["lid"] for x in plan["keep"]]
    )
    assert sorted(acted) == sorted(item["lid"] for item in current)
    # 再算一次结果完全一致（确定性）
    again = plan_reconciliation(current, batch_lids, {"L1": "active", "L3": "off_market", "L5": "sold"})
    assert again == plan


def test_plan_empty_batch_withdraws_everything():
    current = [{"lid": "L1"}, {"lid": "L2"}]
    plan = plan_reconciliation(current, set(), {})
    assert sorted(item["lid"] for item in plan["withdraw"]) == ["L1", "L2"]
    assert plan["keep"] == plan["deactivate"] == []
