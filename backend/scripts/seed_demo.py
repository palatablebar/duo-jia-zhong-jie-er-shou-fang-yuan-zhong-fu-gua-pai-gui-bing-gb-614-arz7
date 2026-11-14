"""幂等 seed：3 账号 / 9 中介 / 540 条挂牌 / 快照批次 / 2 次归并重算 / 386 个房源簇 / 6 张复核单。

自然键 upsert：
  users.email / agencies.code / snapshot_batches(agency_id, batch_key) /
  listings(agency_id, lid) / match_runs.seed_key / reviews.seed_key / audit_logs.seed_key
重复执行不新增、不覆盖用户改过的复核结论；SEED_SUMMARY 逐字一致。

历史挂牌通过一次「基线整批快照」接入，补齐首次/最近出现批次等快照生命周期字段，
保证只让当前有效挂牌（is_current=True）进入归并。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from src.config.database import session_scope
from src.models.agency import Agency
from src.models.audit import AuditLog
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.property import Property
from src.models.review import Review
from src.models.snapshot import ListingSnapshot, SnapshotBatch
from src.models.user import User
from src.services import match_run_service, review_service
from src.utils.security import hash_password
from src.utils.timeutil import utcnow

from scripts.demo_config import (
    AGENCIES,
    DEMO_PASSWORD,
    DEMO_USERS,
    ORG,
    REVIEW_LIMIT,
    SEED_RUNS,
    build_listings,
)

BASELINE_SNAPSHOT_DATE = date(2026, 11, 1)
BASELINE_BATCH_KEY = "seed-baseline-20261101"


def ensure_users(db) -> int:
    for email, display_name, role in DEMO_USERS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(DEMO_PASSWORD),
                    display_name=display_name,
                    role=role,
                    org=ORG,
                )
            )
    db.flush()
    return db.scalar(select(func.count()).select_from(User)) or 0


def ensure_agencies(db) -> int:
    for spec in AGENCIES:
        agency = db.scalar(select(Agency).where(Agency.code == spec["code"]))
        if agency is None:
            db.add(
                Agency(
                    code=spec["code"],
                    name=spec["name"],
                    kind=spec["kind"],
                    endpoint=spec.get("endpoint"),
                    owner=ORG,
                    note=spec.get("note"),
                    config=spec.get("config") or {},
                    enabled=True,
                )
            )
    db.flush()
    return db.scalar(select(func.count()).select_from(Agency)) or 0


def ensure_listings(db) -> int:
    """按 (agency_id, lid) 幂等写入 540 条原始挂牌（已存在的不覆盖）。"""
    agencies = {agency.code: agency for agency in db.scalars(select(Agency)).all()}
    existing = {
        (agency_id, lid)
        for agency_id, lid in db.execute(select(Listing.agency_id, Listing.lid)).all()
    }
    _, rows = build_listings()
    created = 0
    for row in rows:
        agency = agencies.get(row["agency_code"])
        if agency is None:
            continue
        key = (agency.id, row["lid"])
        if key in existing:
            continue
        db.add(
            Listing(
                agency_id=agency.id,
                lid=row["lid"],
                community_raw=row["community_raw"],
                layout_raw=row["layout_raw"],
                area_raw=row["area_raw"],
                price_raw=row["price_raw"],
                floor_raw=row["floor_raw"],
                orientation_raw=row["orientation_raw"],
                title=row["title"],
                status=row["status"],
                listed_days=row["listed_days"],
                views=row["views"],
            )
        )
        existing.add(key)
        created += 1
    db.flush()
    return created


def ensure_snapshots(db) -> int:
    """为每家中介补一条基线整批快照（幂等），并回填挂牌的快照生命周期字段。

    与真实接入同一套自然键：批次已存在则跳过，不新增任何记录。
    """
    agencies = {agency.code: agency for agency in db.scalars(select(Agency)).all()}
    _, rows = build_listings()
    rows_by_agency: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_agency.setdefault(row["agency_code"], []).append(row)

    now = utcnow()
    batch_count = 0
    for agency_code, agency_rows in rows_by_agency.items():
        agency = agencies.get(agency_code)
        if agency is None:
            continue
        exists = db.scalar(
            select(SnapshotBatch).where(
                SnapshotBatch.agency_id == agency.id,
                SnapshotBatch.batch_key == BASELINE_BATCH_KEY,
            )
        )
        if exists is not None:
            continue
        batch = SnapshotBatch(
            agency_id=agency.id,
            batch_key=BASELINE_BATCH_KEY,
            snapshot_date=BASELINE_SNAPSHOT_DATE,
            status="applied",
            row_count=len(agency_rows),
            applied_at=now,
            summary={
                "snapshots_received": len(agency_rows),
                "created": len(agency_rows),
                "reactivated": 0,
                "kept": 0,
                "withdrawn": 0,
                "deactivated": 0,
                "stale_ignored": 0,
            },
        )
        db.add(batch)
        db.flush()
        lid_to_listing = {
            listing.lid: listing
            for listing in db.scalars(select(Listing).where(Listing.agency_id == agency.id)).all()
        }
        for row in agency_rows:
            db.add(
                ListingSnapshot(
                    batch_id=batch.id,
                    agency_id=agency.id,
                    lid=row["lid"],
                    community_raw=row["community_raw"],
                    layout_raw=row["layout_raw"],
                    area_raw=row["area_raw"],
                    price_raw=row["price_raw"],
                    floor_raw=row["floor_raw"],
                    orientation_raw=row["orientation_raw"],
                    title=row["title"],
                    status=row["status"],
                    listed_days=row["listed_days"],
                    views=row["views"],
                    captured_at=now,
                    raw_payload={key: row[key] for key in row},
                )
            )
            listing = lid_to_listing.get(row["lid"])
            if listing is not None and listing.first_seen_batch_id is None:
                listing.first_seen_batch_id = batch.id
                listing.last_seen_batch_id = batch.id
                listing.is_current = True
        batch_count += 1
    db.flush()
    return batch_count


def ensure_runs(db) -> int:
    for seed_key, trigger, operator, threshold in SEED_RUNS:
        match_run_service.create_and_execute(
            db,
            trigger=trigger,
            operator_email=operator,
            threshold=threshold,
            seed_key=seed_key,
        )
    return db.scalar(select(func.count()).select_from(MatchRun)) or 0


def ensure_reviews(db) -> int:
    """给风险最高的若干簇（高/中风险）各开一张复核单；已有复核单的不再新增。"""
    run = db.scalar(select(MatchRun).order_by(MatchRun.id.desc()).limit(1))
    props = list(
        db.scalars(
            select(Property)
            .where(Property.risk_level.in_(["high", "medium"]))
            .order_by(Property.risk_score.desc(), Property.id.asc())
            .limit(REVIEW_LIMIT)
        ).all()
    )
    for prop in props:
        review_service.ensure_for_property(
            db,
            prop=prop,
            run_id=run.id if run else None,
            seed_key=f"seed-review-{prop.cluster_key[:16]}",
        )
    db.flush()
    return db.scalar(select(func.count()).select_from(Review)) or 0


def ensure_audit(db) -> None:
    seeds = [
        ("seed-audit-1", "admin@listingmerge.io", "admin", "agency.create", "agency", {"code": "lianjia"}),
        ("seed-audit-2", "analyst@listingmerge.io", "analyst", "run.create", "match_run", {"run": "seed-run-baseline"}),
        ("seed-audit-3", "analyst@listingmerge.io", "analyst", "review.submit", "review", {"note": "初始化复核"}),
    ]
    for seed_key, email, role, action, entity, detail in seeds:
        exists = db.scalar(select(AuditLog).where(AuditLog.seed_key == seed_key))
        if exists is None:
            db.add(
                AuditLog(
                    actor_email=email,
                    actor_role=role,
                    action=action,
                    entity=entity,
                    detail=detail,
                    layer="seed",
                    seed_key=seed_key,
                )
            )
    db.flush()


def seed_all() -> dict:
    with session_scope() as db:
        users = ensure_users(db)
        agencies = ensure_agencies(db)
        ensure_listings(db)
        ensure_snapshots(db)
        runs = ensure_runs(db)
        ensure_reviews(db)
        ensure_audit(db)
        clusters = db.scalar(select(func.count()).select_from(Property)) or 0
        pending = db.scalar(select(func.count()).select_from(Review).where(Review.status == "pending")) or 0
        summary = (
            f"SEED_SUMMARY brand=listingmerge users={users} sources={agencies} "
            f"runs={runs} findings={clusters} pending={pending}"
        )
        print(summary, flush=True)
        return {"users": users, "sources": agencies, "runs": runs, "findings": clusters, "pending": pending}


if __name__ == "__main__":
    seed_all()
