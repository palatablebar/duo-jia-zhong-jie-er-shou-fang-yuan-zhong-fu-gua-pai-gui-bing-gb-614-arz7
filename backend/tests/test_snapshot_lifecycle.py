"""快照接入生命周期的数据库集成测试（SQLite 内存库，无需 MySQL/Postgres）。

覆盖需求口径：
- 完整历史 + 幂等：重试同一批不新增任何记录；
- 只让当前有效挂牌进入归并（撤回 / 下架 / 成交出簇）；
- 晚到旧批次只归档，不复活已撤回挂牌；
- 撤回后换新编号重挂：旧编号留失效依据，新编号是当前有效；
- 重挂后再跑流水线，簇成员 / 中介数随快照同步变化。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.models import Base
from src.models.agency import Agency
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.snapshot import ListingSnapshot, SnapshotBatch
from src.services import pipeline_service, snapshot_service


def _row(lid: str, **overrides) -> dict:
    base = {
        "lid": lid,
        "community_raw": "翠湖名邸",
        "layout_raw": "3室2厅1卫",
        "area_raw": "89.5平米",
        "price_raw": "600万",
        "floor_raw": "6/18",
        "orientation_raw": "南北通透",
        "title": "89.5平 南北 近地铁",
        "status": "active",
        "listed_days": 30,
        "views": 10,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        agency = Agency(code="lianjia", name="链家测试店", kind="broker")
        session.add(agency)
        session.flush()
        session.commit()
        yield session


def _agency_id(db: Session) -> int:
    return db.scalar(select(Agency.id))


def _ingest(db: Session, batch_key: str, snapshot_date: date, rows):
    return snapshot_service.ingest_batch(
        db,
        agency_id=_agency_id(db),
        batch_key=batch_key,
        snapshot_date=snapshot_date,
        rows=rows,
    )


def test_first_batch_creates_current_listings_and_history(db: Session):
    result = _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2")])
    db.commit()
    assert result["duplicate"] is False and result["stale"] is False
    assert result["summary"]["created"] == 2
    # 挂牌全部当前有效，且带批次溯源
    listings = db.scalars(select(Listing)).all()
    assert {l.lid for l in listings} == {"L1", "L2"}
    assert all(l.is_current for l in listings)
    assert all(l.first_seen_batch_id is not None for l in listings)
    # 完整历史：批次与批次内快照各一条
    assert db.scalar(select(func.count()).select_from(SnapshotBatch)) == 1
    assert db.scalar(select(func.count()).select_from(ListingSnapshot)) == 2


def test_retry_same_batch_is_idempotent_no_new_rows(db: Session):
    _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2")])
    db.commit()
    # 重试同一批（即便报文完全一致）
    retry = _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2")])
    db.commit()
    assert retry["duplicate"] is True
    assert db.scalar(select(func.count()).select_from(SnapshotBatch)) == 1
    assert db.scalar(select(func.count()).select_from(ListingSnapshot)) == 2
    assert db.scalar(select(func.count()).select_from(Listing)) == 2


def test_withdraw_marks_invalid_with_reason_and_excludes_from_merge(db: Session):
    _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2")])
    db.commit()
    # 新整批快照：L2 撤回（不再出现），L1 保留
    result = _ingest(db, "b-0002", date(2026, 11, 2), [_row("L1")])
    db.commit()
    assert result["summary"]["withdrawn"] == 1
    withdrawn = db.scalar(select(Listing).where(Listing.lid == "L2"))
    assert withdrawn.is_current is False
    assert withdrawn.invalid_reason == "withdrawn"
    assert withdrawn.invalid_batch_id is not None
    assert withdrawn.invalid_evidence.get("withdrawn_from_batch") == "b-0002"
    assert withdrawn.invalidated_at is not None
    # 历史仍保留（没被删除）
    assert db.scalar(select(func.count()).select_from(Listing)) == 2
    # 只有当前有效挂牌进流水线
    run = MatchRun(run_no="R1", trigger="manual", operator_email="t@x.io")
    db.add(run)
    db.flush()
    out = pipeline_service.run_pipeline(db, run)
    assert out["metrics"]["listings"] == 1
    assert out["metrics"]["inactive_listings"] == 1


def test_off_market_and_sold_are_invalidated_with_distinct_reasons(db: Session):
    _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2"), _row("L3")])
    db.commit()
    _ingest(
        db,
        "b-0002",
        date(2026, 11, 2),
        [_row("L1"), _row("L2", status="off_market"), _row("L3", status="sold")],
    )
    db.commit()
    reasons = {
        lid: reason
        for lid, reason in db.execute(select(Listing.lid, Listing.invalid_reason)).all()
    }
    assert reasons["L2"] == "off_market"
    assert reasons["L3"] == "sold"
    assert reasons["L1"] is None
    current = db.scalars(select(Listing).where(Listing.is_current.is_(True))).all()
    assert {l.lid for l in current} == {"L1"}


def test_late_old_batch_is_archived_and_does_not_resurrect(db: Session):
    # 11-01 批次有 L1/L2
    _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2")])
    db.commit()
    # 11-02 批次撤回 L2
    _ingest(db, "b-0002", date(2026, 11, 2), [_row("L1")])
    db.commit()
    # 晚到的 10-31 旧批次又带上了 L2
    late = _ingest(db, "b-late", date(2026, 10, 31), [_row("L1"), _row("L2")])
    db.commit()
    assert late["stale"] is True
    assert late["summary"]["stale_ignored"] == 2
    # L2 没有被复活
    l2 = db.scalar(select(Listing).where(Listing.lid == "L2"))
    assert l2.is_current is False
    assert l2.invalid_reason == "withdrawn"
    # 旧批次仅归档
    stale_batch = db.scalar(select(SnapshotBatch).where(SnapshotBatch.batch_key == "b-late"))
    assert stale_batch.status == "stale"
    assert stale_batch.applied_at is None


def test_relist_under_new_lid_keeps_old_history_and_new_lid_is_current(db: Session):
    _ingest(db, "b-0001", date(2026, 11, 1), [_row("OLD-1", price_raw="598万")])
    db.commit()
    # 门店撤回 OLD-1，换新编号 NEW-9 重挂同一套房
    result = _ingest(db, "b-0002", date(2026, 11, 2), [_row("NEW-9", price_raw="602万")])
    db.commit()
    assert result["summary"]["withdrawn"] == 1
    assert result["summary"]["created"] == 1
    old = db.scalar(select(Listing).where(Listing.lid == "OLD-1"))
    new = db.scalar(select(Listing).where(Listing.lid == "NEW-9"))
    assert old.is_current is False and old.invalid_reason == "withdrawn"
    assert new.is_current is True and new.invalid_reason is None
    # 两条记录都保留（完整历史），当前有效只有新编号
    assert db.scalar(select(func.count()).select_from(Listing)) == 2
    assert db.scalar(
        select(func.count()).select_from(Listing).where(Listing.is_current.is_(True))
    ) == 1


def test_same_lid_reappears_active_is_reactivated_not_duplicated(db: Session):
    _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2")])
    db.commit()
    # 新批次不再出现 L1（撤回），L2 保留
    _ingest(db, "b-0002", date(2026, 11, 2), [_row("L2")])
    db.commit()
    assert db.scalar(select(Listing).where(Listing.lid == "L1")).is_current is False
    # 后续更新批次里同编号重新在售
    _ingest(db, "b-0003", date(2026, 11, 3), [_row("L1", price_raw="610万"), _row("L2")])
    db.commit()
    l1 = db.scalar(select(Listing).where(Listing.lid == "L1"))
    assert l1.is_current is True
    assert l1.invalid_reason is None and l1.invalid_batch_id is None
    assert l1.price_raw == "610万"  # 用最新快照刷新
    # 没有产生重复 Listing
    assert db.scalar(select(func.count()).select_from(Listing).where(Listing.lid == "L1")) == 1


def test_empty_batch_is_rejected_as_validation_error(db: Session):
    from src.utils.errors import AppError

    with pytest.raises(AppError) as exc:
        _ingest(db, "b-empty", date(2026, 11, 1), [])
    assert exc.value.status_code == 422


def test_pipeline_cluster_members_and_agency_count_track_relisting(db: Session):
    # 两家中介挂同一套房
    a1 = _agency_id(db)
    agency2 = Agency(code="wawj", name="我爱我家测试店", kind="broker")
    db.add(agency2)
    db.flush()
    snapshot_service.ingest_batch(
        db, agency_id=a1, batch_key="a1-b1", snapshot_date=date(2026, 11, 1),
        rows=[_row("A1-X", price_raw="600万")],
    )
    snapshot_service.ingest_batch(
        db, agency_id=agency2.id, batch_key="a2-b1", snapshot_date=date(2026, 11, 1),
        rows=[_row("A2-X", price_raw="602万")],
    )
    db.commit()
    run = MatchRun(run_no="R1", trigger="manual", operator_email="t@x.io")
    db.add(run)
    db.flush()
    pipeline_service.run_pipeline(db, run)
    db.flush()
    from src.models.property import Property

    multi = db.scalars(select(Property).where(Property.member_count > 1)).all()
    assert len(multi) == 1
    assert multi[0].member_count == 2 and multi[0].agency_count == 2

    # 中介 1 撤回旧编号、换新编号重挂
    snapshot_service.ingest_batch(
        db, agency_id=a1, batch_key="a1-b2", snapshot_date=date(2026, 11, 2),
        rows=[_row("A1-NEW", price_raw="601万")],
    )
    db.commit()
    run2 = MatchRun(run_no="R2", trigger="manual", operator_email="t@x.io")
    db.add(run2)
    db.flush()
    pipeline_service.run_pipeline(db, run2)
    db.flush()
    multi = db.scalars(select(Property).where(Property.member_count > 1)).all()
    # 旧编号已出簇，新编号入簇：仍是 2 成员 / 2 中介，但成员编号已换
    assert len(multi) == 1
    assert multi[0].member_count == 2 and multi[0].agency_count == 2
    member_lids = {l.lid for l in db.scalars(select(Listing).where(Listing.property_id == multi[0].id)).all()}
    assert member_lids == {"A1-NEW", "A2-X"}
    # 旧编号留痕且不再挂任何簇
    old = db.scalar(select(Listing).where(Listing.lid == "A1-X"))
    assert old.is_current is False and old.property_id is None


def test_rerun_pipeline_reassigns_kept_listings_without_dangling_property_id(db: Session):
    """回归：保留下来的当前有效挂牌在旧簇被淘汰后，必须重新挂到新簇（property_id 不能残留 NULL）。"""
    a1 = _agency_id(db)
    agency2 = Agency(code="wawj", name="我爱我家测试店", kind="broker")
    db.add(agency2)
    db.flush()
    snapshot_service.ingest_batch(
        db, agency_id=a1, batch_key="b1", snapshot_date=date(2026, 11, 1), rows=[_row("X1")]
    )
    snapshot_service.ingest_batch(
        db, agency_id=agency2.id, batch_key="b2", snapshot_date=date(2026, 11, 1), rows=[_row("Y1")]
    )
    db.commit()
    for run_no in ("R1", "R2"):
        run = MatchRun(run_no=run_no, trigger="manual", operator_email="t@x.io")
        db.add(run)
        db.flush()
        pipeline_service.run_pipeline(db, run)
        db.flush()
    # 每条当前有效挂牌都必须挂到某个簇，不存在悬空 property_id
    dangling = db.scalar(
        select(func.count())
        .select_from(Listing)
        .where(Listing.is_current.is_(True), Listing.property_id.is_(None))
    )
    assert dangling == 0


def test_reconciliation_export_keeps_invalidation_basis_for_every_listing(db: Session):
    from src.services import export_service

    _ingest(db, "b-0001", date(2026, 11, 1), [_row("L1"), _row("L2")])
    db.commit()
    _ingest(db, "b-0002", date(2026, 11, 2), [_row("L1"), _row("L3", status="off_market")])
    db.commit()
    content = export_service.listings_csv(db).decode("utf-8")
    header = content.split("\r\n", 1)[0]
    for column in ("是否当前有效", "失效依据", "失效批次", "失效证据"):
        assert column in header
    rows = {ln.split(",", 1)[0]: ln for ln in content.split("\r\n") if ln and not ln.startswith("﻿挂牌编号")}
    # 完整历史：L2 撤回、L3 新建即下架、L1 仍有效
    assert set(rows) == {"L1", "L2", "L3"}
    assert "门店撤回" in rows["L2"] and "b-0002" in rows["L2"]
    assert "中介标记已下架" in rows["L3"]
    assert "当前有效" in rows["L1"]
