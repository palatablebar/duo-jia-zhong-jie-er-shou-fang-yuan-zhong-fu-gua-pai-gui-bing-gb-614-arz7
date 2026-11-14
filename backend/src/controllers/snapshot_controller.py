"""快照批次控制器：接入写操作记领域审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, snapshot_service


def ingest_batch(db: Session, payload, principal: dict, request: Request) -> dict:
    data = payload.model_dump()
    rows = [row for row in data["rows"]]
    result = snapshot_service.ingest_batch(
        db,
        agency_id=data["agency_id"],
        batch_key=data["batch_key"],
        snapshot_date=data["snapshot_date"],
        rows=rows,
        note=data.get("note"),
    )
    batch = result["batch"]
    if result["duplicate"]:
        action = "snapshot.retry"
    elif result["stale"]:
        action = "snapshot.stale"
    else:
        action = "snapshot.ingest"
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action=action,
        entity="snapshot_batch",
        entity_id=batch.id,
        detail={
            "agency_id": batch.agency_id,
            "batch_key": batch.batch_key,
            "snapshot_date": batch.snapshot_date.isoformat() if batch.snapshot_date else None,
            "status": batch.status,
            "summary": result["summary"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return {
        "batch": snapshot_service.serialize_batch(batch),
        "summary": result["summary"],
        "duplicate": result["duplicate"],
        "stale": result["stale"],
    }


def list_batches(db: Session, **filters) -> dict:
    return snapshot_service.list_batches(db, **filters)
