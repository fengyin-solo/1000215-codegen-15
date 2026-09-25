"""杀青结算业务规则：状态流转、字段校验、批量核对与幂等扣减都收在这里。"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.store import store

MODULE = "wrap"
REQUIRED_FIELDS = ["结算单号", "结算对象", "结算周期"]
STATUS_ORDER = ["待核对", "核对中", "已确认", "已付清", "有争议"]
ACTION_RULES = {"发起核对": "核对中", "确认结算": "已付清", "标记争议": "有争议"}
NEGATIVE_ACTIONS = []

# 批量核对的五种整组结果：差额、争议、缺失属于失败项，允许只重试这些对象；
# 已锁定是已付清单据的只读态，不允许核对改写，也不算失败。
RESULT_PASS = "通过"
RESULT_DIFF = "差额"
RESULT_DISPUTE = "争议"
RESULT_MISSING = "缺失"
RESULT_LOCKED = "已锁定"
FAIL_RESULTS = (RESULT_DIFF, RESULT_DISPUTE, RESULT_MISSING)
RESULT_LABELS = (RESULT_PASS, RESULT_DIFF, RESULT_DISPUTE, RESULT_MISSING, RESULT_LOCKED)

SETTLED_STATUS = "已付清"

# 结算周期里允许出现的日期写法：2026-03-01 / 2026/03/01 / 2026年3月1日，可由 ~、至、- 等连接两个日期。
_DATE_PATTERN = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?")


def _parse_amount(value: Any) -> float | None:
    """把金额字段解析成不小于 0 的数字；空值、文字描述（如「待定」）、负数都视为数据缺失。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        text = str(value).strip().replace(",", "").replace("，", "")
        if not text:
            return None
        if text.endswith("元"):
            text = text[:-1].strip()
        try:
            amount = float(text)
        except ValueError:
            return None
    return round(amount, 2) if amount >= 0 else None


def _parse_period(value: Any) -> tuple[list[date] | None, str]:
    """识别结算周期里的起止日期；单个日期也算有效周期，无法识别或起止颠倒时给出原因。"""
    text = str(value or "").strip()
    matches = list(_DATE_PATTERN.finditer(text))
    if not matches:
        return None, "结算周期缺少可识别的起止日期"
    if len(matches) > 2:
        return None, "结算周期出现多个日期段，无法确定起止范围"
    dates: list[date] = []
    for match in matches:
        try:
            dates.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            return None, f"结算周期日期不合法：{match.group(0).strip()}"
    if len(dates) == 2 and dates[0] > dates[1]:
        return None, "结算周期起始日晚于结束日"
    return dates, ""


class WrapService:
    def __init__(self) -> None:
        # 批量核对账本：batch_no -> {round, items: {id: 结果快照}}。
        # 同一批次重复提交时按账本回放，保证不重复扣减、不重复改写。
        self._batches: dict[str, dict[str, Any]] = {}

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        result: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("结算单号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if result:
            # 列表、争议区、导出都按行上落库的「核对结果」过滤，三处口径天然一致。
            rows = [row for row in rows if row.get("核对结果") == result]
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
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于杀青结算可执行范围"
        if entry.get("status") == SETTLED_STATUS:
            return None, "结算单已付清，不能再改写状态"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"结算单已{action}"

    # ------------------------------------------------------------------
    # 批量核对
    # ------------------------------------------------------------------

    def batch_check(
        self, entry_ids: list[int], batch_no: str | None = None
    ) -> dict[str, Any] | None:
        """对多选结算对象逐条核对；返回整组结果。空选择返回 None 由接口层转成可读提示。"""
        ids = list(dict.fromkeys(int(entry_id) for entry_id in entry_ids))
        if not ids:
            return None

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        batch = self._batches.get(batch_no or "")
        if batch is None:
            batch_no = batch_no or self._new_batch_no()
            batch = {"batch_no": batch_no, "round": 0, "created_at": now, "items": {}}
            self._batches[batch_no] = batch
            mode = "new"
        elif set(ids) == set(batch["items"]):
            # 一次提交重复执行：整组原样回放账本，不重新校验、不扣减、不改写。
            return self._batch_payload(
                batch,
                replayed=True,
                message=f"批次 {batch_no} 与上次提交完全相同，结果按账本回放，未重复扣减",
            )
        else:
            mode = "retry"

        batch["round"] += 1
        round_no = int(batch["round"])
        targets = ids
        for entry_id in targets:
            snapshot = batch["items"].get(entry_id)
            # 通过项已经扣减过，之后任何重试都只回放快照，杜绝重复扣减与改写。
            if snapshot is not None and snapshot["核对结果"] == RESULT_PASS:
                continue
            batch["items"][entry_id] = self._evaluate(entry_id, batch_no, round_no, now)

        checked = len(targets)
        if mode == "new":
            message = (
                f"批量核对完成，本轮核对 {checked} 个对象，"
                f"整组结果与扣减金额见下方明细"
            )
        else:
            message = f"第 {round_no} 轮核对完成，仅重试 {checked} 个对象，其余结果沿用上轮"
        return self._batch_payload(batch, replayed=False, message=message)

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        """读取历史批次结果（同样来自账本，不触发任何校验与扣减）。"""
        batch = self._batches.get(batch_no)
        if batch is None:
            return None
        return self._batch_payload(
            batch,
            replayed=True,
            message=f"批次 {batch_no} 结果按账本回放，未重复扣减",
        )

    def _new_batch_no(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        seq = sum(1 for key in self._batches if key.startswith(f"CHK-{stamp}")) + 1
        return f"CHK-{stamp}-{seq:03d}"

    def _evaluate(
        self, entry_id: int, batch_no: str, round_no: int, now: str
    ) -> dict[str, Any]:
        """逐条核对一张结算单：金额、周期、已付关系依次校验，并在单据上落核对结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return self._snapshot(
                entry_id, None, round_no, RESULT_MISSING, None, 0.0,
                "未找到结算单（可能已删除或归档），保留原状态",
            )

        # 已付清：整单只读，任何字段都不写。
        if entry.get("status") == SETTLED_STATUS:
            return self._snapshot(
                entry_id, entry, round_no, RESULT_LOCKED, None, 0.0,
                "结算单已付清，核对不改写",
            )

        payable = _parse_amount(entry.get("应结金额"))
        paid = _parse_amount(entry.get("已付金额"))
        if payable is None or paid is None:
            # 数据缺失：只落核对标记，业务状态与金额原样保留，补齐后可直接重试。
            self._write_markers(entry, batch_no, RESULT_MISSING, round_no, now,
                                "应结金额或已付金额缺失/无法识别，保留原状态")
            return self._snapshot(entry_id, entry, round_no, RESULT_MISSING, None, 0.0,
                                  entry["核对说明"])

        diff = round(payable - paid, 2)
        _dates, period_error = _parse_period(entry.get("结算周期"))
        if period_error:
            return self._finish(
                entry, batch_no, round_no, now,
                result=RESULT_DISPUTE, diff=diff, deducted=0.0,
                target_status="有争议", note=period_error,
            )
        if paid > payable:
            return self._finish(
                entry, batch_no, round_no, now,
                result=RESULT_DISPUTE, diff=diff, deducted=0.0,
                target_status="有争议",
                note=f"已付金额超出应结金额 {abs(diff):.2f} 元，需人工争议处理",
            )
        if diff == 0:
            # 核对通过：按已付金额结清未付。先看单据上登记的未付金额，没有就按应结-已付推算。
            outstanding = _parse_amount(entry.get("未付金额"))
            if outstanding is None:
                outstanding = max(payable - paid, 0.0)
            deducted = round(max(outstanding, 0.0), 2)
            entry["未付金额"] = 0.0
            return self._finish(
                entry, batch_no, round_no, now,
                result=RESULT_PASS, diff=0.0, deducted=deducted,
                target_status="已确认",
                note=f"应结与已付一致，未付金额扣减 {deducted:.2f} 元后结清",
            )

        # 已付小于应结：差额待补付，不改金额，只推进到核对中。
        return self._finish(
            entry, batch_no, round_no, now,
            result=RESULT_DIFF, diff=diff, deducted=0.0,
            target_status="核对中",
            note=f"已付金额不足，差额 {diff:.2f} 元，补付后可重试",
        )

    def _finish(
        self,
        entry: dict[str, Any],
        batch_no: str,
        round_no: int,
        now: str,
        *,
        result: str,
        diff: float | None,
        deducted: float,
        target_status: str,
        note: str,
    ) -> dict[str, Any]:
        self._write_markers(entry, batch_no, result, round_no, now, note)
        entry["status"] = target_status
        entry["pending"] = target_status != STATUS_ORDER[-1]
        entry["abnormal"] = target_status == "有争议"
        return self._snapshot(int(entry["id"]), entry, round_no, result, diff, deducted, note)

    @staticmethod
    def _write_markers(
        entry: dict[str, Any],
        batch_no: str,
        result: str,
        round_no: int,
        now: str,
        note: str,
    ) -> None:
        entry["核对批次"] = batch_no
        entry["核对结果"] = result
        entry["核对轮次"] = round_no
        entry["核对时间"] = now
        entry["核对说明"] = note

    @staticmethod
    def _snapshot(
        entry_id: int,
        entry: dict[str, Any] | None,
        round_no: int,
        result: str,
        diff: float | None,
        deducted: float,
        note: str,
    ) -> dict[str, Any]:
        """固化批次明细，重复提交时回放的就是这份快照（含当时金额与扣减额）。"""
        return {
            "id": entry_id,
            "结算单号": entry.get("结算单号") if entry is not None else None,
            "结算对象": entry.get("结算对象") if entry is not None else None,
            "结算周期": entry.get("结算周期") if entry is not None else None,
            "应结金额": entry.get("应结金额") if entry is not None else None,
            "已付金额": entry.get("已付金额") if entry is not None else None,
            "核对结果": result,
            "核对差额": diff,
            "扣减额": deducted,
            "核对轮次": round_no,
            "核对说明": note,
        }

    @staticmethod
    def _batch_payload(
        batch: dict[str, Any], *, replayed: bool, message: str
    ) -> dict[str, Any]:
        items = list(batch["items"].values())
        summary: dict[str, Any] = {"合计": len(items)}
        for label in RESULT_LABELS:
            summary[label] = sum(1 for item in items if item["核对结果"] == label)
        summary["失败项"] = sum(
            1 for item in items if item["核对结果"] in FAIL_RESULTS
        )
        summary["扣减额"] = round(
            sum(float(item["扣减额"]) for item in items), 2
        )
        return {
            "ok": True,
            "message": message,
            "batch_no": batch["batch_no"],
            "round": batch["round"],
            "replayed": replayed,
            "summary": summary,
            "items": items,
            "failed_ids": [
                int(item["id"]) for item in items if item["核对结果"] in FAIL_RESULTS
            ],
        }
