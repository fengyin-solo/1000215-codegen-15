"""杀青结算接口：维护结算单，覆盖批量核对、确认结算、标记争议等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, BatchCheckPayload, BatchCheckResult, EntryPayload, PageResult
from app.services.wrap import RESULT_LABELS
from app.services.wrap import WrapService

router = APIRouter(prefix="/api/wrap", tags=["杀青结算"])

service = WrapService()

LIST_FIELDS = ["结算单号", "结算对象", "结算周期", "应结金额", "已付金额", "未付金额", "结算人", "结算状态"]
STATUSES = ["待核对", "核对中", "已确认", "已付清", "有争议"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按结算单号检索"),
    status: str | None = Query(default=None, description="待核对、核对中、已确认、已付清、有争议"),
    result: str | None = Query(default=None, description="核对结果：通过、差额、争议、缺失、已锁定"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按结算单号、状态与核对结果过滤杀青结算列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if result is not None and result not in RESULT_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"核对结果只支持：{'、'.join(RESULT_LABELS)}",
        )
    items, total = service.list_entries(
        keyword=keyword, status=status, result=result, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/batch-check", response_model=BatchCheckResult)
def batch_check(payload: BatchCheckPayload) -> BatchCheckResult:
    """多选结算对象批量核对；整组逐条标记，重复提交回放账本，失败对象可带原批次号重试。"""
    if not payload.entry_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个结算对象再发起核对")
    result = service.batch_check(payload.entry_ids, payload.batch_no)
    return BatchCheckResult.model_validate(result)


@router.get("/batch-check/{batch_no}", response_model=BatchCheckResult)
def get_batch(batch_no: str) -> BatchCheckResult:
    """读取历史批次核对结果；只读回放，不触发重复扣减。"""
    result = service.get_batch(batch_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"核对批次 {batch_no} 不存在")
    return BatchCheckResult.model_validate(result)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出杀青结算清单：返回含核对结果的全量数据，与列表、争议区展示同一口径。"""
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


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条结算单执行发起核对、确认结算、标记争议；已付清单据不允许改写。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
