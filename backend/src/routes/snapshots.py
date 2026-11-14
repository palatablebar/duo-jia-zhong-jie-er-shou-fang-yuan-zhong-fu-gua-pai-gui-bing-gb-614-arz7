from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import READ_ROLES, RUN_CREATE_ROLES
from src.controllers import snapshot_controller
from src.middlewares.rbac import require_roles
from src.schemas.snapshot import SnapshotBatchIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.get("/batches")
def list_batches(
    agency_id: int | None = None,
    status: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return snapshot_controller.list_batches(
        db, agency_id=agency_id, status=status, limit=page.limit, offset=page.offset
    )


@router.post("/batches", status_code=201)
def ingest_batch(
    payload: SnapshotBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*RUN_CREATE_ROLES)),
) -> dict:
    return snapshot_controller.ingest_batch(db, payload, principal, request)
