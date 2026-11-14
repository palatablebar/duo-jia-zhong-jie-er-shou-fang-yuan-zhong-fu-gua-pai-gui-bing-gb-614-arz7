"""整批快照接入与对账（核心领域服务）。

中介补发的是**整批快照**，本服务保证：

1. 完整历史：``snapshot_batches`` + ``listing_snapshots`` 只追加，保留每一批原文；
2. 幂等接入：自然键 ``(agency_id, batch_key)`` 去重，重试同一批不新增任何记录；
3. 旧批不复活：晚到的旧批次（``snapshot_date`` 早于该渠道当前批次）只归档为
   ``stale``，不修改任何当前有效挂牌，已撤回挂牌不会被旧记录恢复；
4. 只让当前有效挂牌进入归并：对账把撤回 / 下架 / 成交的挂牌置
   ``is_current=False`` 并写失效依据，换新编号重挂产生新 Listing。

对账的分类规则集中在纯函数 ``plan_reconciliation``，不访问数据库，便于单测。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    INVALID_REASON_LABELS,
    SNAPSHOT_BATCH_STATUS_LABELS,
    InvalidReason,
    ListingStatus,
    SnapshotBatchStatus,
)
from src.models.agency import Agency
from src.models.listing import Listing
from src.models.snapshot import ListingSnapshot, SnapshotBatch
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, parse_date, utcnow

# 快照行允许携带的挂牌原文列（与 Listing 的原始字段对齐）
SNAPSHOT_ROW_FIELDS = (
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
    "title",
    "status",
    "listed_days",
    "views",
)
_REQUIRED_ROW_FIELDS = (
    "lid",
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
)

# 快照自报状态 -> 失效依据（active 不失效）
_STATUS_INVALID_REASON = {
    ListingStatus.OFF.value: InvalidReason.OFF_MARKET.value,
    ListingStatus.SOLD.value: InvalidReason.SOLD.value,
}


# --------------------------------------------------------------------------- #
# 纯函数：批次新旧判定 + 对账计划（无 DB，无 IO，确定性）
# --------------------------------------------------------------------------- #
def is_stale_batch(snapshot_date: date, latest_date: date | None) -> bool:
    """晚到的旧批次：归属快照日早于该渠道已对账的最新批次。

    同快照日的批次不算旧（同一天可能多渠道文件重推），仍走幂等/对账。
    """
    return latest_date is not None and snapshot_date < latest_date


def plan_reconciliation(
    current_listings: list[dict[str, Any]],
    batch_lids: set[str],
    batch_status: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """对一家中介的「当前有效挂牌」与「新批次编号集合」做对账分类。

    入参：
      current_listings: 该中介 ``is_current=True`` 的挂牌，至少含 ``lid``；
      batch_lids:       新整批快照里出现的全部编号；
      batch_status:     ``lid -> 快照自报 status``。

    返回四类动作（均为数据载体，不在此处落库）：
      withdraw:  当前有效但新批次不再出现          -> 撤回（withdrawn）
      deactivate: 当前有效、新批次出现但自报下架/成交 -> off_market / sold
      keep:      当前有效且新批次仍为在售           -> 维持有效
      revive:    已失效但新批次重新出现且自报在售    -> 重新激活（同编号回流）
    「新编号重挂」无法仅凭本函数判定（要查历史是否存在），由服务层补 ``create``。
    """
    plan: dict[str, list[dict[str, Any]]] = {
        "withdraw": [],
        "deactivate": [],
        "keep": [],
        "revive": [],
    }
    for listing in current_listings:
        lid = listing["lid"]
        if lid not in batch_lids:
            plan["withdraw"].append({"lid": lid, "reason": InvalidReason.WITHDRAWN.value})
            continue
        reason = _STATUS_INVALID_REASON.get(batch_status.get(lid, ListingStatus.ACTIVE.value))
        if reason is not None:
            plan["deactivate"].append({"lid": lid, "reason": reason, "status": batch_status[lid]})
        else:
            plan["keep"].append({"lid": lid})
    return plan


def empty_summary() -> dict[str, int]:
    return {
        "snapshots_received": 0,
        "created": 0,
        "reactivated": 0,
        "kept": 0,
        "withdrawn": 0,
        "deactivated": 0,
        "stale_ignored": 0,
    }


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _validate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise AppError("VALIDATION", 422, {"rows": "批次至少包含一条挂牌快照"})
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AppError("VALIDATION", 422, {"row": index, "reason": "每行必须是对象"})
        missing = [field for field in _REQUIRED_ROW_FIELDS if not str(row.get(field) or "").strip()]
        if missing:
            raise AppError("VALIDATION", 422, {"row": index, "missing": missing})
        lid = str(row["lid"]).strip()
        if lid in seen:
            raise AppError("VALIDATION", 422, {"row": index, "lid": lid, "reason": "批次内编号重复"})
        seen.add(lid)
        status = str(row.get("status") or ListingStatus.ACTIVE.value).strip()
        if status not in (ListingStatus.ACTIVE.value, ListingStatus.SOLD.value, ListingStatus.OFF.value):
            raise AppError("VALIDATION", 422, {"row": index, "lid": lid, "status": status})
        cleaned.append(row)
    return cleaned


# --------------------------------------------------------------------------- #
# 服务层
# --------------------------------------------------------------------------- #
def _get_agency(db: Session, agency_id: int) -> Agency:
    agency = db.get(Agency, agency_id)
    if agency is None:
        raise not_found("agency", agency_id)
    return agency


def _latest_applied_date(db: Session, agency_id: int) -> date | None:
    return db.scalar(
        select(func.max(SnapshotBatch.snapshot_date)).where(
            SnapshotBatch.agency_id == agency_id,
            SnapshotBatch.status == SnapshotBatchStatus.APPLIED.value,
        )
    )


def get_batch(db: Session, agency_id: int, batch_key: str) -> SnapshotBatch | None:
    return db.scalar(
        select(SnapshotBatch).where(
            SnapshotBatch.agency_id == agency_id, SnapshotBatch.batch_key == batch_key
        )
    )


def ingest_batch(
    db: Session,
    *,
    agency_id: int,
    batch_key: str,
    snapshot_date: date | str,
    rows: list[dict[str, Any]],
    note: str | None = None,
) -> dict[str, Any]:
    """接入一批整批快照并完成对账（幂等、可安全重试）。

    返回 ``{"batch": SnapshotBatch, "summary": dict, "duplicate": bool, "stale": bool}``。
    """
    agency = _get_agency(db, agency_id)
    batch_key = str(batch_key or "").strip()
    if not batch_key:
        raise AppError("VALIDATION", 422, {"batch_key": "必填"})
    snap_date = parse_date(snapshot_date) if isinstance(snapshot_date, str) else snapshot_date
    if not isinstance(snap_date, date):
        raise AppError("VALIDATION", 422, {"snapshot_date": snapshot_date})
    rows = _validate_rows(rows)

    # 1) 幂等：同一 (agency, batch_key) 重试，原样返回，不新增任何记录
    existing = get_batch(db, agency_id, batch_key)
    if existing is not None:
        return {
            "batch": existing,
            "summary": dict(existing.summary or empty_summary()),
            "duplicate": True,
            "stale": existing.status == SnapshotBatchStatus.STALE.value,
        }

    # 2) 晚到的旧批次：只归档留痕，绝不修改当前有效挂牌（防止旧记录复活撤回挂牌）
    latest_date = _latest_applied_date(db, agency_id)
    stale = is_stale_batch(snap_date, latest_date)

    batch = SnapshotBatch(
        agency_id=agency_id,
        batch_key=batch_key,
        snapshot_date=snap_date,
        status=SnapshotBatchStatus.STALE.value if stale else SnapshotBatchStatus.APPLIED.value,
        row_count=len(rows),
        note=note,
        summary=empty_summary(),
    )
    db.add(batch)
    db.flush()  # 拿到 batch.id

    # 3) 完整历史：批次原文只追加
    now = utcnow()
    for row in rows:
        db.add(
            ListingSnapshot(
                batch_id=batch.id,
                agency_id=agency_id,
                lid=str(row["lid"]).strip(),
                community_raw=str(row["community_raw"]).strip(),
                layout_raw=str(row["layout_raw"]).strip(),
                area_raw=str(row["area_raw"]).strip(),
                price_raw=str(row["price_raw"]).strip(),
                floor_raw=str(row["floor_raw"]).strip(),
                orientation_raw=str(row["orientation_raw"]).strip(),
                title=str(row.get("title") or "").strip(),
                status=str(row.get("status") or ListingStatus.ACTIVE.value).strip(),
                listed_days=int(row.get("listed_days") or 0),
                views=int(row.get("views") or 0),
                captured_at=now,
                raw_payload={field: row.get(field) for field in SNAPSHOT_ROW_FIELDS if field in row},
            )
        )

    summary = empty_summary()
    summary["snapshots_received"] = len(rows)

    if stale:
        # 旧批只留痕：不动 listings，计数体现在 stale_ignored
        summary["stale_ignored"] = len(rows)
        batch.summary = summary
        batch.applied_at = None
        db.flush()
        return {"batch": batch, "summary": summary, "duplicate": False, "stale": True}

    # 4) 对账：只针对该中介的挂牌
    batch_lids = {str(row["lid"]).strip() for row in rows}
    batch_status = {str(row["lid"]).strip(): str(row.get("status") or ListingStatus.ACTIVE.value).strip()
                    for row in rows}
    row_by_lid = {str(row["lid"]).strip(): row for row in rows}

    current_rows = db.scalars(
        select(Listing).where(Listing.agency_id == agency_id, Listing.is_current.is_(True))
    ).all()
    plan = plan_reconciliation(
        [{"lid": item.lid} for item in current_rows], batch_lids, batch_status
    )
    current_by_lid = {item.lid: item for item in current_rows}

    # 4a) 撤回：新批次不再出现的当前有效挂牌
    for action in plan["withdraw"]:
        _invalidate(
            current_by_lid[action["lid"]],
            reason=action["reason"],
            batch=batch,
            evidence={"withdrawn_from_batch": batch_key, "snapshot_date": snap_date.isoformat()},
        )
        summary["withdrawn"] += 1

    # 4b) 下架 / 成交：新批次出现但自报非在售
    for action in plan["deactivate"]:
        listing = current_by_lid[action["lid"]]
        _invalidate(
            listing,
            reason=action["reason"],
            batch=batch,
            evidence={
                "reported_status": action["status"],
                "batch_key": batch_key,
                "snapshot_date": snap_date.isoformat(),
            },
        )
        listing.status = action["status"]
        summary["deactivated"] += 1

    # 4c) 维持有效：刷新最后出现批次
    for action in plan["keep"]:
        listing = current_by_lid[action["lid"]]
        listing.last_seen_batch_id = batch.id
        _refresh_listing(listing, row_by_lid[action["lid"]])
        summary["kept"] += 1

    # 4d) 新编号 / 同编号回流：查该中介全部历史挂牌
    handled_lids = set(current_by_lid)
    historical = db.scalars(
        select(Listing).where(Listing.agency_id == agency_id, Listing.is_current.is_(False))
    ).all()
    historical_by_lid = {item.lid: item for item in historical}
    for lid in sorted(batch_lids):
        if lid in handled_lids:
            continue
        reported = batch_status.get(lid, ListingStatus.ACTIVE.value)
        reason = _STATUS_INVALID_REASON.get(reported)
        old = historical_by_lid.get(lid)
        if old is not None and reason is None:
            # 同编号重新在售：重新激活（晚到旧批不会走到这里，stale 已提前拦截）
            _reactivate(old, batch)
            _refresh_listing(old, row_by_lid[lid])
            summary["reactivated"] += 1
            continue
        # 全新编号（撤回后换新编号重挂落在这条）；自报下架/成交的新编号也建历史并立即失效
        new_listing = _build_listing(agency_id, row_by_lid[lid], batch, now)
        db.add(new_listing)
        db.flush()
        if reason is not None:
            _invalidate(
                new_listing,
                reason=reason,
                batch=batch,
                evidence={"reported_status": reported, "batch_key": batch_key,
                          "snapshot_date": snap_date.isoformat(), "born_inactive": True},
            )
        summary["created"] += 1

    batch.summary = summary
    batch.applied_at = utcnow()
    agency.last_synced_at = utcnow()
    db.flush()
    return {"batch": batch, "summary": summary, "duplicate": False, "stale": False}


def _invalidate(listing: Listing, *, reason: str, batch: SnapshotBatch, evidence: dict) -> None:
    """把挂牌置为失效并留痕（不删除，保留完整历史与失效依据）。"""
    listing.is_current = False
    listing.invalid_reason = reason
    listing.invalid_batch_id = batch.id
    listing.invalid_evidence = evidence
    listing.invalidated_at = utcnow()
    listing.property_id = None  # 失效即出簇，下轮归并不再计入
    listing.risk_tags = []


def _reactivate(listing: Listing, batch: SnapshotBatch) -> None:
    """同编号重新在售：清掉失效依据，恢复为当前有效。"""
    listing.is_current = True
    listing.invalid_reason = None
    listing.invalid_batch_id = None
    listing.invalid_evidence = {}
    listing.invalidated_at = None
    listing.first_seen_batch_id = listing.first_seen_batch_id or batch.id
    listing.last_seen_batch_id = batch.id


def _build_listing(agency_id: int, row: dict, batch: SnapshotBatch, now: datetime) -> Listing:
    return Listing(
        agency_id=agency_id,
        lid=str(row["lid"]).strip(),
        community_raw=str(row["community_raw"]).strip(),
        layout_raw=str(row["layout_raw"]).strip(),
        area_raw=str(row["area_raw"]).strip(),
        price_raw=str(row["price_raw"]).strip(),
        floor_raw=str(row["floor_raw"]).strip(),
        orientation_raw=str(row["orientation_raw"]).strip(),
        title=str(row.get("title") or "").strip(),
        status=str(row.get("status") or ListingStatus.ACTIVE.value).strip(),
        listed_days=int(row.get("listed_days") or 0),
        views=int(row.get("views") or 0),
        captured_at=now,
        is_current=True,
        first_seen_batch_id=batch.id,
        last_seen_batch_id=batch.id,
    )


def _refresh_listing(listing: Listing, row: dict) -> None:
    """批次再次确认在售时，用最新快照刷新原文与读数（归一结果由流水线重算）。"""
    for field in SNAPSHOT_ROW_FIELDS:
        if field in row and row[field] is not None:
            setattr(listing, field, row[field])
    listing.status = str(row.get("status") or ListingStatus.ACTIVE.value).strip()


# --------------------------------------------------------------------------- #
# 查询 / 序列化
# --------------------------------------------------------------------------- #
def serialize_batch(batch: SnapshotBatch, agency: Agency | None = None) -> dict[str, Any]:
    return {
        "id": batch.id,
        "agency_id": batch.agency_id,
        "agency_name": agency.name if agency else "",
        "agency_code": agency.code if agency else "",
        "batch_key": batch.batch_key,
        "snapshot_date": batch.snapshot_date.isoformat() if batch.snapshot_date else None,
        "status": batch.status,
        "status_label": SNAPSHOT_BATCH_STATUS_LABELS.get(batch.status, batch.status),
        "row_count": batch.row_count,
        "summary": batch.summary or empty_summary(),
        "note": batch.note,
        "received_at": iso_utc(batch.received_at),
        "applied_at": iso_utc(batch.applied_at),
        "created_at": iso_utc(batch.created_at),
    }


def list_batches(
    db: Session,
    *,
    agency_id: int | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(SnapshotBatch)
    count_stmt = select(func.count()).select_from(SnapshotBatch)
    if agency_id:
        stmt = stmt.where(SnapshotBatch.agency_id == agency_id)
        count_stmt = count_stmt.where(SnapshotBatch.agency_id == agency_id)
    if status:
        stmt = stmt.where(SnapshotBatch.status == status)
        count_stmt = count_stmt.where(SnapshotBatch.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(SnapshotBatch.id.desc()).limit(limit).offset(offset)).all()
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    return {
        "items": [serialize_batch(row, agencies.get(row.agency_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def invalid_reason_label(reason: str | None) -> str:
    if not reason:
        return ""
    return INVALID_REASON_LABELS.get(reason, reason)
