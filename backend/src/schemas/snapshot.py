from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class SnapshotRowIn(BaseModel):
    """中介整批快照里的一条挂牌原文（保留采集口径，不在此处归一）。"""

    lid: str = Field(min_length=1, max_length=40)
    community_raw: str = Field(min_length=1, max_length=160)
    layout_raw: str = Field(min_length=1, max_length=60)
    area_raw: str = Field(min_length=1, max_length=60)
    price_raw: str = Field(min_length=1, max_length=60)
    floor_raw: str = Field(min_length=1, max_length=60)
    orientation_raw: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default="", max_length=200)
    status: Literal["active", "sold", "off_market"] = "active"
    listed_days: int = Field(default=0, ge=0)
    views: int = Field(default=0, ge=0)


class SnapshotBatchIn(BaseModel):
    """整批快照接入请求；同 ``(agency_id, batch_key)`` 重试幂等。"""

    agency_id: int
    batch_key: str = Field(min_length=2, max_length=120)
    snapshot_date: date
    note: str | None = Field(default=None, max_length=2000)
    rows: list[SnapshotRowIn] = Field(min_length=1)
