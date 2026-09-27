"""备用电源检查的纯判定逻辑。

与 app.py 中的检查记录存储、计划页组装分离：本模块只负责
"检查时刻 + 可持续时长 + 截止时间 + 余量" 是否仍然有效，
不读写数据库，便于单独核验。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# 余量下限（MW）：低于该值即判定余量不足
MARGIN_FLOOR_MW = 0.0

# 判定状态：
# ok           检查有效且余量充足
# missing      尚未登记检查
# expired      已过截止时间（检查失效）
# insufficient 余量不足（可能与 expired 同时出现，state 取更严重的 expired）
# stale        确认所依据的检查结果或用电需求已被更新，原确认失效
STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_EXPIRED = "expired"
STATE_INSUFFICIENT = "insufficient"
STATE_STALE = "stale"


def parse_time(value: str | datetime) -> datetime:
    """解析 ISO8601 时间（兼容结尾 Z），无时区按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_time(dt: datetime) -> str:
    return parse_time(dt).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute_deadline(checked_at: str, sustainable_minutes: int) -> str:
    """截止时间 = 检查时刻 + 可持续时长。"""
    minutes = int(sustainable_minutes)
    if minutes <= 0:
        raise ValueError("可持续时长必须为正整数分钟")
    return format_time(parse_time(checked_at) + timedelta(minutes=minutes))


def remaining_minutes(deadline: str, at: str | datetime) -> int:
    """距离截止时间还剩多少分钟（负数表示已超时）。"""
    delta = parse_time(deadline) - parse_time(at)
    return int(delta.total_seconds() // 60)


@dataclass
class BackupVerdict:
    state: str
    valid: bool
    margin_mw: float | None = None
    remaining_minutes: int | None = None
    deadline: str | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"state": self.state, "valid": self.valid, "margin_mw": self.margin_mw,
                "remaining_minutes": self.remaining_minutes, "deadline": self.deadline,
                "reasons": list(self.reasons)}


def missing(reason: str = "备用电源检查尚未登记") -> BackupVerdict:
    return BackupVerdict(STATE_MISSING, False, reasons=[reason])


def stale(reason: str) -> BackupVerdict:
    return BackupVerdict(STALE, False, reasons=[reason])


def evaluate(*, deadline: str, backup_mw: float, demand_mw: float,
             at: str | datetime) -> BackupVerdict:
    """依据一条检查记录判定当前是否有效。

    检查失效：当前时刻已超过截止时间。
    余量不足：备用可用容量低于用电需求（低于 MARGIN_FLOOR_MW）。
    两类原因可同时给出。
    """
    deadline_dt = parse_time(deadline)
    at_dt = parse_time(at)
    expired = at_dt > deadline_dt
    margin = round(float(backup_mw) - float(demand_mw), 6)
    left = int((deadline_dt - at_dt).total_seconds() // 60)
    reasons: list[str] = []
    if expired:
        reasons.append(f"备用电源检查已失效（截止时间 {deadline}）")
    if margin < MARGIN_FLOOR_MW:
        reasons.append(f"备用余量不足：缺口 {abs(margin):g}MW（备用 {backup_mw:g}MW／用电需求 {demand_mw:g}MW）")
    if not reasons:
        state = STATE_OK
    elif expired:
        state = STATE_EXPIRED
    else:
        state = STATE_INSUFFICIENT
    return BackupVerdict(state, not reasons, margin_mw=margin, remaining_minutes=left,
                         deadline=deadline, reasons=reasons)
