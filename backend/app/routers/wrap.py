"""杀青结算接口：维护结算单，覆盖批量核对、失败重试、确认结算、标记争议等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, BatchVerifyResult, EntryPayload, PageResult
from app.services.wrap import WrapService

router = APIRouter(prefix="/api/wrap", tags=["杀青结算"])

service = WrapService()

LIST_FIELDS = ["结算单号", "结算对象", "结算周期", "应结金额", "已付金额", "未付金额", "结算人", "结算状态", "核对结果", "核对说明"]
STATUSES = ["待核对", "核对中", "已确认", "已付清", "有争议"]


class BatchVerifyPayload(BaseModel):
    """批量核对入参：结算对象编号集合与保证幂等的批次号。"""

    ids: list[int] = Field(default_factory=list)
    batch_id: str = ""
    only_failed: bool = False
    retry_of: str = ""


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按结算单号检索"),
    status: str | None = Query(default=None, description="待核对、核对中、已确认、已付清、有争议"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按结算单号与状态过滤杀青结算列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/disputes")
def list_disputes() -> dict[str, Any]:
    """争议区：与列表、导出同一口径，集中展示被标记为争议的结算单。"""
    items = service.list_disputes()
    return {"module": "wrap", "total": len(items), "items": items}


@router.get("/batch/{batch_id}", response_model=BatchVerifyResult)
def get_batch(batch_id: str) -> BatchVerifyResult:
    """按批次号回放一组核对结果，供刷新页面后继续重试失败项。"""
    snapshot = service.get_batch(batch_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"核对批次 {batch_id} 不存在或已过期")
    return BatchVerifyResult(**snapshot)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出杀青结算清单：列表、争议区与导出共用同一份核对结果字段。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "wrap", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条结算单明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"结算单 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条结算单，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="结算单已登记", entry=entry)


@router.post("/batch-verify", response_model=BatchVerifyResult)
def batch_verify(payload: BatchVerifyPayload) -> BatchVerifyResult:
    """批量核对多选结算对象；同批次重复提交只回放结果，不重复执行扣减。

    only_failed=true 时只重试上一批的失败项（差额、争议），并以新批次号独立存档。
    """
    ids = payload.ids
    if payload.only_failed:
        source_id = payload.retry_of or payload.batch_id
        previous = service.get_batch(source_id) if source_id else None
        if previous is None:
            raise HTTPException(status_code=400, detail="没有可重试的核对批次，请先整组核对")
        ids = previous["failed_ids"]
        # 重试产生新批次：新 batch_id 与原批次互不影响，为空时由服务端生成
        batch_id = payload.batch_id
        if not ids:
            raise HTTPException(status_code=400, detail="上一批没有失败项，无需重试")
    else:
        batch_id = payload.batch_id
    try:
        snapshot = service.verify_batch(ids, batch_id=batch_id or _new_batch_id())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BatchVerifyResult(**snapshot)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条结算单执行发起核对、确认结算、标记争议；已付清终态不允许改写。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


def _new_batch_id() -> str:
    import uuid

    return f"BATCH-{uuid.uuid4().hex[:12]}"
