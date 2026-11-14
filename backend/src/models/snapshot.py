"""中介整批挂牌快照接入层。

中介每天补发的是「整批快照」：同一门店撤回旧编号后会换新编号重挂。
两张表保留**完整历史**且接入幂等：

- ``SnapshotBatch`` 批次：自然键 ``(agency_id, batch_key)``，重试同一批不新增；
  晚到的旧批次标记 ``stale``，仅归档、不驱动挂牌生命周期。
- ``ListingSnapshot`` 批次内的每条原始快照：自然键 ``(batch_id, lid)``，
  只追加不覆盖，是对账与失效依据的留痕来源。

当前有效挂牌（``listings.is_current=True``）由 ``snapshot_service`` 对账得出，
归并流水线只读这部分；失效挂牌留在 ``listings`` 里并带失效依据。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class SnapshotBatch(Base):
    __tablename__ = "snapshot_batches"
    __table_args__ = (
        UniqueConstraint("agency_id", "batch_key", name="uq_snapshot_batches_agency_batch"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agency_id: Mapped[int] = mapped_column(
        ForeignKey("agencies.id", ondelete="CASCADE"), index=True
    )
    # 中介/采集侧的批次唯一标识（文件名、推送流水号等）；同一 batch_key 重试幂等
    batch_key: Mapped[str] = mapped_column(String(120), index=True)
    # 批次归属快照日：判断新旧批次的业务口径（晚到旧批不会复活已撤回挂牌）
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    # applied / stale / duplicate，见 constants.enums.SnapshotBatchStatus
    status: Mapped[str] = mapped_column(String(20), default="applied", index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # applied 批次对账后的生命周期计数；stale/duplicate 保持全 0
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ListingSnapshot(Base):
    __tablename__ = "listing_snapshots"
    __table_args__ = (
        UniqueConstraint("batch_id", "lid", name="uq_listing_snapshots_batch_lid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("snapshot_batches.id", ondelete="CASCADE"), index=True
    )
    agency_id: Mapped[int] = mapped_column(
        ForeignKey("agencies.id", ondelete="CASCADE"), index=True
    )
    lid: Mapped[str] = mapped_column(String(40), index=True)
    community_raw: Mapped[str] = mapped_column(String(160))
    layout_raw: Mapped[str] = mapped_column(String(60))
    area_raw: Mapped[str] = mapped_column(String(60))
    price_raw: Mapped[str] = mapped_column(String(60))
    floor_raw: Mapped[str] = mapped_column(String(60))
    orientation_raw: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    listed_days: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # 原始报文留档（完整历史，不参与计算）
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
