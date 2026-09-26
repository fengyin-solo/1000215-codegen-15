"""杀青结算业务规则：状态流转、批量核对口径与幂等处理都收在这里。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store

MODULE = "wrap"
REQUIRED_FIELDS = ["结算单号", "结算对象", "结算周期"]
STATUS_ORDER = ["待核对", "核对中", "已确认", "已付清", "有争议"]
ACTION_RULES = {"发起核对": "核对中", "确认结算": "已付清", "标记争议": "有争议"}
NEGATIVE_ACTIONS = []

# 批量核对结果标记：通过 / 差额 / 争议；已付清结算单不参与改写，单独跳过。
VERIFY_PASS = "通过"
VERIFY_DIFF = "差额"
VERIFY_DISPUTE = "争议"
VERIFY_SKIP = "跳过"
FAILED_RESULTS = {VERIFY_DIFF, VERIFY_DISPUTE}
RESULT_LABEL = {
    VERIFY_PASS: "核对通过",
    VERIFY_DIFF: "金额差额",
    VERIFY_DISPUTE: "存在争议",
    VERIFY_SKIP: "已付清未核对",
}
SETTLED_STATUS = "已付清"


def _to_amount(value: Any) -> float | None:
    """把金额字段解析成数字；空值或无法识别的数字一律视为数据缺失，不猜测。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_period(value: Any) -> tuple[date | None, date | None]:
    """解析结算周期，支持单日或「2026-01-01~2026-01-31」这类起止区间。"""
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None

    def parse_day(raw: str) -> date | None:
        raw = raw.strip()
        if not raw:
            return None
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
            try:
                return datetime.strptime(raw, pattern).date()
            except ValueError:
                continue
        return None

    single = parse_day(text)
    if single is not None:
        return single, single

    for separator in ("~", "～", "至", "到", "—", "~"):
        if separator in text:
            left, _, right = text.partition(separator)
            start = parse_day(left)
            end = parse_day(right) if right.strip() else start
            return start, end
    return None, None


class WrapService:
    def __init__(self) -> None:
        # batch_id -> 最近一次核对结果快照；同批次重复提交直接回放，不重复扣减
        self._batches: dict[str, dict[str, Any]] = {}

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("结算单号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"结算单 {entry_id} 不存在或已归档"
        if entry.get("status") == SETTLED_STATUS:
            return None, "结算单已付清，终态单据不能被改写"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于杀青结算可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["结算状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"结算单已{action}"

    # ---- 批量核对 ------------------------------------------------------

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self._batches.get(batch_id)

    def list_disputes(self) -> list[dict[str, Any]]:
        """争议区口径：最近一次核对被标记为争议、且当前结算单仍未付清的对象。"""
        return [
            row for row in store.rows(MODULE)
            if row.get("核对结果") == VERIFY_DISPUTE and row.get("status") != SETTLED_STATUS
        ]

    def verify_batch(self, entry_ids: list[int], batch_id: str) -> dict[str, Any]:
        """多选结算对象后逐条核对；同批次重复提交回放快照，不重复执行、不重复扣减。"""
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise ValueError("缺少批次号 batch_id，无法保证重复提交幂等")
        if batch_id in self._batches:
            snapshot = self._batches[batch_id]
            return dict(snapshot, idempotent=True)
        if not entry_ids:
            raise ValueError("请先勾选需要核对的结算对象")

        seen: set[int] = set()
        ordered_ids = [item for item in entry_ids if not (item in seen or seen.add(item))]

        results: list[dict[str, Any]] = []
        summary = {VERIFY_PASS: 0, VERIFY_DIFF: 0, VERIFY_DISPUTE: 0, VERIFY_SKIP: 0}
        for entry_id in ordered_ids:
            result = self._verify_one(entry_id)
            results.append(result)
            summary[result["核对结果"]] += 1

        failed_ids = [
            int(item["id"]) for item in results
            if item["核对结果"] in FAILED_RESULTS
        ]
        message = (
            f"本组核对 {len(results)} 个对象：通过 {summary[VERIFY_PASS]}、"
            f"差额 {summary[VERIFY_DIFF]}、争议 {summary[VERIFY_DISPUTE]}"
        )
        if summary[VERIFY_SKIP]:
            message += f"，已付清跳过 {summary[VERIFY_SKIP]}"
        snapshot = {
            "ok": True,
            "message": message,
            "batch_id": batch_id,
            "idempotent": False,
            "summary": summary,
            "results": results,
            "failed_ids": failed_ids,
        }
        self._batches[batch_id] = snapshot
        return snapshot

    def _verify_one(self, entry_id: int) -> dict[str, Any]:
        """逐条校验应结金额、已付金额与结算周期；数据缺失时保留原状态。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return self._mark(
                entry_id,
                None,
                VERIFY_DISPUTE,
                "结算单不存在或已归档",
                keep_status=True,
            )
        if entry.get("status") == SETTLED_STATUS:
            # 已付清结算单是终态：核对只给只读结论，绝不改写金额与状态
            return self._mark(entry_id, entry, VERIFY_SKIP, "结算单已付清，跳过核对", keep_status=True)

        payable = _to_amount(entry.get("应结金额"))
        paid = _to_amount(entry.get("已付金额"))
        missing_fields = [
            name for name, value in (("应结金额", payable), ("已付金额", paid))
            if value is None
        ]
        if not str(entry.get("结算周期") or "").strip():
            missing_fields.append("结算周期")
        if missing_fields:
            return self._mark(
                entry_id,
                entry,
                VERIFY_DISPUTE,
                f"数据缺失（{'、'.join(missing_fields)}），保留原状态",
                keep_status=True,
            )
        if payable is not None and paid is not None and (payable < 0 or paid < 0):
            return self._mark(entry_id, entry, VERIFY_DISPUTE, "金额不能为负，请核实账单", keep_status=True)

        start, end = _parse_period(entry.get("结算周期"))
        if start is None or (end is not None and end < start):
            return self._mark(
                entry_id,
                entry,
                VERIFY_DISPUTE,
                "结算周期无法识别或起止倒置，请补充区间",
                keep_status=True,
            )

        # 金额口径：金额齐全后重算未付金额，保证列表、争议区、导出一致
        unpaid = round(payable - paid, 2)
        entry["未付金额"] = unpaid
        if abs(unpaid) <= 0.009:
            return self._mark(
                entry_id,
                entry,
                VERIFY_PASS,
                "应结与已付金额一致，结算周期有效",
                target_status="已确认",
            )
        if unpaid > 0:
            return self._mark(
                entry_id,
                entry,
                VERIFY_DIFF,
                f"已付不足，差额 {unpaid:.2f}",
                keep_status=True,
            )
        return self._mark(
            entry_id,
            entry,
            VERIFY_DISPUTE,
            f"已付超出应结 {abs(unpaid):.2f}，需复核",
            keep_status=True,
        )

    def _mark(
        self,
        entry_id: int,
        entry: dict[str, Any] | None,
        result: str,
        reason: str,
        *,
        keep_status: bool = False,
        target_status: str | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": entry_id,
            "结算单号": entry.get("结算单号") if entry else None,
            "结算对象": entry.get("结算对象") if entry else None,
            "结算周期": entry.get("结算周期") if entry else None,
            "应结金额": entry.get("应结金额") if entry else None,
            "已付金额": entry.get("已付金额") if entry else None,
            "未付金额": entry.get("未付金额") if entry else None,
            "核对结果": result,
            "核对说明": reason,
            "status": entry.get("status") if entry else None,
        }
        if entry is not None:
            entry["核对结果"] = result
            entry["核对说明"] = reason
            if keep_status:
                # 数据缺失 / 差额 / 争议 / 已付清：保留原状态，不做流转
                entry["abnormal"] = entry.get("abnormal", False) or result == VERIFY_DISPUTE
            elif target_status:
                entry["status"] = target_status
                entry["结算状态"] = target_status
                entry["pending"] = False
                entry["abnormal"] = False
            item["status"] = entry.get("status")
        return item
