"""导出服务：归并簇 / 挂牌明细 / 复核单 / 对账 CSV（含两个算法的结果列）。

口径区分：
- 归并簇 CSV（银行抵押估价用）：只由当前有效挂牌算出，撤回 / 下架不进簇；
- 挂牌明细 CSV：保留完整历史，当前有效与失效挂牌都在，并带每条挂牌的失效依据，
  满足「对账结果保留每条挂牌的失效依据」。
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import (
    INVALID_REASON_LABELS,
    LISTING_STATUS_LABELS,
    REVIEW_REASON_LABELS,
    REVIEW_STATUS_LABELS,
    RISK_LEVEL_LABELS,
    RISK_TAG_LABELS,
)
from src.models.agency import Agency
from src.models.listing import Listing
from src.models.property import Property
from src.models.review import Review
from src.models.snapshot import SnapshotBatch
from src.utils.csv_export import to_csv
from src.utils.timeutil import local_stamp


def _tags(tags: list[str] | None) -> str:
    return " / ".join(RISK_TAG_LABELS.get(tag, tag) for tag in (tags or []))


def clusters_csv(db: Session) -> bytes:
    props = db.scalars(select(Property).order_by(Property.risk_score.desc(), Property.id)).all()
    headers = [
        "簇编号",
        "小区",
        "户型",
        "面积众数(㎡)",
        "价格中位(元)",
        "簇置信",
        "成员挂牌数",
        "中介数",
        "价格极差(万元)",
        "离散度",
        "风险分",
        "风险等级",
        "风险标签",
        "钓鱼价挂牌",
        "僵尸挂牌",
        "更新时间",
    ]
    rows = []
    for prop in props:
        detail = prop.risk_detail or {}
        rows.append(
            [
                prop.id,
                prop.community,
                prop.layout,
                f"{prop.area_mode:g}",
                prop.price_median,
                f"{prop.confidence:.3f}",
                prop.member_count,
                prop.agency_count,
                f"{prop.spread / 10000:.1f}",
                f"{prop.dispersion:.4f}",
                prop.risk_score,
                RISK_LEVEL_LABELS.get(prop.risk_level, prop.risk_level),
                _tags(prop.risk_tags),
                " / ".join(detail.get("bait_lids") or []) or "-",
                " / ".join(detail.get("stale_lids") or []) or "-",
                local_stamp(prop.updated_at),
            ]
        )
    return to_csv(headers, rows)


def listings_csv(db: Session, agency_id: int | None = None) -> bytes:
    """挂牌明细对账 CSV：完整历史（含失效挂牌），每条带失效依据与来源批次。"""
    stmt = select(Listing)
    if agency_id:
        stmt = stmt.where(Listing.agency_id == agency_id)
    listings = db.scalars(stmt.order_by(Listing.agency_id, Listing.id)).all()
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    props = {prop.id: prop for prop in db.scalars(select(Property)).all()}
    batches = {batch.id: batch.batch_key for batch in db.scalars(select(SnapshotBatch)).all()}
    headers = [
        "挂牌编号",
        "中介",
        "小区(原文)",
        "小区(归一)",
        "面积(原文)",
        "面积(㎡)",
        "价格(原文)",
        "价格(元)",
        "楼层(原文)",
        "楼层",
        "朝向(归一)",
        "户型(归一)",
        "业务状态",
        "是否当前有效",
        "失效依据",
        "失效批次",
        "失效证据",
        "失效时间",
        "首次出现批次",
        "最近出现批次",
        "挂牌天数",
        "带看次数",
        "所属簇",
        "簇风险分",
        "风险标签",
        "归一标记",
    ]
    rows = []
    for listing in listings:
        prop = props.get(listing.property_id)
        rows.append(
            [
                listing.lid,
                agencies.get(listing.agency_id).name if agencies.get(listing.agency_id) else "",
                listing.community_raw,
                listing.community_norm,
                listing.area_raw,
                f"{listing.area_num:g}" if listing.area_num is not None else "",
                listing.price_raw,
                listing.price_num or "",
                listing.floor_raw,
                listing.floor_num or "",
                listing.orientation_norm,
                listing.layout_norm,
                LISTING_STATUS_LABELS.get(listing.status, listing.status),
                "当前有效" if listing.is_current else "已失效",
                INVALID_REASON_LABELS.get(listing.invalid_reason, listing.invalid_reason or "")
                if not listing.is_current else "",
                batches.get(listing.invalid_batch_id, "") if listing.invalid_batch_id else "",
                json.dumps(listing.invalid_evidence, ensure_ascii=False) if listing.invalid_evidence else "",
                local_stamp(listing.invalidated_at) if listing.invalidated_at else "",
                batches.get(listing.first_seen_batch_id, "") if listing.first_seen_batch_id else "",
                batches.get(listing.last_seen_batch_id, "") if listing.last_seen_batch_id else "",
                listing.listed_days,
                listing.views,
                prop.id if prop else "",
                prop.risk_score if prop else "",
                _tags(listing.risk_tags),
                " / ".join(listing.parse_flags or []),
            ]
        )
    return to_csv(headers, rows)


def reviews_csv(db: Session, status: str | None = None) -> bytes:
    stmt = select(Review)
    if status:
        stmt = stmt.where(Review.status == status)
    reviews = db.scalars(stmt.order_by(Review.id)).all()
    props = {prop.id: prop for prop in db.scalars(select(Property)).all()}
    headers = ["复核号", "小区", "户型", "风险分", "风险等级", "复核原因", "状态", "复核人", "结论备注", "创建时间"]
    rows = []
    for review in reviews:
        prop = props.get(review.property_id)
        rows.append(
            [
                review.id,
                prop.community if prop else "",
                prop.layout if prop else "",
                review.risk_score,
                RISK_LEVEL_LABELS.get(review.severity, review.severity),
                REVIEW_REASON_LABELS.get(review.reason, review.reason),
                REVIEW_STATUS_LABELS.get(review.status, review.status),
                review.reviewer or "",
                review.note or "",
                local_stamp(review.created_at),
            ]
        )
    return to_csv(headers, rows)
