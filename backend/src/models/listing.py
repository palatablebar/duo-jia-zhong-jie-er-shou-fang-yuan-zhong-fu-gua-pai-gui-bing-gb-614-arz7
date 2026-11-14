"""一条原始挂牌：保留采集原文，同时存归一结果（归一由 backend 计算）。

生命周期（由快照对账驱动，见 ``services/snapshot_service.py``）：
- ``is_current=True`` 的挂牌才是当前有效挂牌，允许进入归并流水线；
- 门店撤回 / 中介标记下架或成交时，``is_current`` 置 False 并留失效依据
  （``invalid_reason`` + 失效批次 + 证据），记录本身保留为完整历史；
- 撤回后换新编号重挂会产生一条新 Listing，旧编号不会被晚到的旧快照复活。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("agency_id", "lid", name="uq_listings_agency_lid"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="CASCADE"), index=True)
    lid: Mapped[str] = mapped_column(String(40), index=True)
    community_raw: Mapped[str] = mapped_column(String(160))
    layout_raw: Mapped[str] = mapped_column(String(60))
    area_raw: Mapped[str] = mapped_column(String(60))
    price_raw: Mapped[str] = mapped_column(String(60))
    floor_raw: Mapped[str] = mapped_column(String(60))
    orientation_raw: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    listed_days: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # ---- 快照生命周期（对账写入）----
    # 是否当前有效挂牌：只有 True 才进入归并；False 为已失效历史，保留留痕。
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    first_seen_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshot_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_seen_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshot_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invalid_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshot_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # withdrawn / off_market / sold / stale_batch，见 constants.enums.InvalidReason
    invalid_reason: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # 失效证据：对账时刻的批次键、上一次出现批次、快照自报状态等
    invalid_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 归一结果（算法 ②「字段解析归一」写入；重算时整表刷新）
    community_norm: Mapped[str] = mapped_column(String(160), default="", index=True)
    layout_norm: Mapped[str] = mapped_column(String(40), default="")
    area_num: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    price_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_band: Mapped[str] = mapped_column(String(10), default="")
    orientation_norm: Mapped[str] = mapped_column(String(20), default="")
    title_tokens: Mapped[list] = mapped_column(JSON, default=list)
    parse_flags: Mapped[list] = mapped_column(JSON, default=list)

    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", ondelete="SET NULL"), nullable=True, index=True
    )
    risk_tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
