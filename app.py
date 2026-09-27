#!/usr/bin/env python3
"""Grid outage restoration planning, field-report merge and status publishing demo."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import backup as backup_rules
from backup import STATE_OK

DB_PATH = Path(__file__).with_name("data.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False); self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL"); self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS assets (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          asset_type TEXT NOT NULL, capacity_mw REAL NOT NULL, parent_id INTEGER REFERENCES assets(id), region TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS facilities (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, facility_type TEXT NOT NULL,
          asset_id INTEGER NOT NULL REFERENCES assets(id), priority INTEGER NOT NULL, backup_power_mw REAL NOT NULL,
          connected INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS outages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, incident_code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('reported','assessing','restoring','restored')),
          affected_regions_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
          opened_by TEXT NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans (
          id INTEGER PRIMARY KEY AUTOINCREMENT, outage_id INTEGER NOT NULL REFERENCES outages(id),
          version INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','submitted','approved','active','superseded')),
          steps_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT, activated_at TEXT,
          UNIQUE(outage_id,version)
        );
        CREATE TABLE IF NOT EXISTS confirmations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES plans(id), step_no INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('confirmed','blocked')), confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL,
          note TEXT,
          backup_check_id INTEGER REFERENCES backup_checks(id), demand_mw_snapshot REAL,
          UNIQUE(plan_id,step_no)
        );
        CREATE TABLE IF NOT EXISTS backup_checks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES plans(id),
          step_no INTEGER NOT NULL, revision_kind TEXT NOT NULL CHECK(revision_kind IN ('check','demand')),
          checked_at TEXT NOT NULL, sustainable_minutes INTEGER NOT NULL, deadline TEXT NOT NULL,
          backup_mw REAL NOT NULL, demand_mw REAL NOT NULL,
          checked_by TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_backup_checks_step ON backup_checks(plan_id,step_no,id);
        CREATE TABLE IF NOT EXISTS field_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, client_report_id TEXT UNIQUE NOT NULL, plan_id INTEGER NOT NULL REFERENCES plans(id),
          step_no INTEGER NOT NULL, expected_plan_version INTEGER NOT NULL, status TEXT NOT NULL,
          note TEXT, merge_status TEXT NOT NULL CHECK(merge_status IN ('merged','conflict','protected')),
          conflict_reason TEXT, reported_by TEXT NOT NULL, received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telemetry (
          id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL REFERENCES assets(id), load_mw REAL NOT NULL,
          voltage_kv REAL NOT NULL, timestamp TEXT NOT NULL, valid INTEGER NOT NULL, anomaly TEXT,
          recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS published_status (
          id INTEGER PRIMARY KEY AUTOINCREMENT, outage_id INTEGER NOT NULL REFERENCES outages(id),
          plan_id INTEGER NOT NULL REFERENCES plans(id), version INTEGER NOT NULL, status_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
        );
        """)
        for col, decl in (("backup_check_id", "INTEGER"), ("demand_mw_snapshot", "REAL")):
            have = self.conn.execute("PRAGMA table_info(confirmations)").fetchall()
            if col not in {row["name"] for row in have}:
                self.conn.execute(f"ALTER TABLE confirmations ADD COLUMN {col} {decl}")
        self.conn.commit()

    def audit(self, actor: str, action: str, entity_type: str, entity_id: object, details: dict) -> None:
        self.conn.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                          (now(), actor, action, entity_type, str(entity_id), j(details)))

    def close(self) -> None: self.conn.close()


class GridService:
    def __init__(self, store: Store): self.store, self.conn = store, store.conn

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: int) -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if not row: raise ApiError(404, "对象不存在")
        return row

    def register_asset(self, actor: str | None, role: str | None, code: str, name: str, asset_type: str, capacity_mw: float, region: str, parent_id: int | None = None) -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        if not code.strip() or not region.strip() or capacity_mw <= 0: raise ApiError(400, "线路资产参数不合法")
        if parent_id is not None: self._row("assets", int(parent_id))
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO assets(code,name,asset_type,capacity_mw,parent_id,region) VALUES(?,?,?,?,?,?)",
                                        (code, name, asset_type, float(capacity_mw), parent_id, region))
                self.store.audit(actor, "asset.register", "asset", cur.lastrowid, {"code": code, "capacity_mw": capacity_mw, "region": region})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "资产代号已存在") from exc
        return {"id": cur.lastrowid, "code": code, "name": name, "asset_type": asset_type, "capacity_mw": capacity_mw, "region": region, "parent_id": parent_id}

    def register_facility(self, actor: str | None, role: str | None, name: str, facility_type: str, asset_id: int, priority: int, backup_power_mw: float) -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        self._row("assets", asset_id)
        if priority not in {1, 2, 3} or backup_power_mw < 0: raise ApiError(400, "重要用户参数不合法")
        with self.conn:
            cur = self.conn.execute("INSERT INTO facilities(name,facility_type,asset_id,priority,backup_power_mw) VALUES(?,?,?,?,?)", (name, facility_type, asset_id, priority, backup_power_mw))
            self.store.audit(actor, "facility.register", "facility", cur.lastrowid, {"name": name, "priority": priority})
        return {"id": cur.lastrowid, "name": name, "facility_type": facility_type, "asset_id": asset_id, "priority": priority, "backup_power_mw": backup_power_mw}

    def create_outage(self, actor: str | None, role: str | None, incident_code: str, title: str, affected_regions: list[str]) -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        if not incident_code.strip() or not affected_regions: raise ApiError(400, "事故编号和影响区域不能为空")
        existing = self.conn.execute("SELECT * FROM outages WHERE incident_code=?", (incident_code,)).fetchone()
        if existing:
            if existing["title"] == title and json.loads(existing["affected_regions_json"]) == affected_regions:
                return self._outage_dict(existing)
            raise ApiError(409, "事故编号已存在但内容不同")
        with self.conn:
            cur = self.conn.execute("INSERT INTO outages(incident_code,title,state,affected_regions_json,opened_by,opened_at,updated_at) VALUES(?,?,'reported',?,?,?,?)",
                                    (incident_code, title, j(affected_regions), actor, now(), now()))
            self.store.audit(actor, "outage.create", "outage", cur.lastrowid, {"incident_code": incident_code})
        return self._outage_dict(self._row("outages", cur.lastrowid))

    def record_telemetry(self, actor: str | None, role: str | None, asset_id: int, load_mw: float, voltage_kv: float, timestamp: str) -> dict:
        actor = self._actor(actor, role, {"operator"})
        asset = self._row("assets", asset_id)
        anomaly = None
        if load_mw < 0 or voltage_kv <= 0: anomaly = "负荷或电压超出物理范围"
        elif load_mw > float(asset["capacity_mw"]) * 1.2: anomaly = "负载超过额定容量20%"
        elif voltage_kv > 500: anomaly = "电压测量值超出本地范围"
        with self.conn:
            cur = self.conn.execute("INSERT INTO telemetry(asset_id,load_mw,voltage_kv,timestamp,valid,anomaly,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (asset_id, load_mw, voltage_kv, timestamp, int(anomaly is None), anomaly, actor, now()))
            self.store.audit(actor, "telemetry.record", "asset", asset_id, {"valid": anomaly is None, "anomaly": anomaly})
        return {"id": cur.lastrowid, "asset_id": asset_id, "load_mw": load_mw, "voltage_kv": voltage_kv, "valid": anomaly is None, "anomaly": anomaly}

    def create_plan(self, actor: str | None, role: str | None, outage_id: int, steps: list[dict]) -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        outage = self._row("outages", outage_id)
        normalized = self._validate_steps(steps)
        version = self.conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM plans WHERE outage_id=?", (outage_id,)).fetchone()[0]
        with self.conn:
            cur = self.conn.execute("INSERT INTO plans(outage_id,version,state,steps_json,created_by,created_at) VALUES(?,?, 'draft',?,?,?)",
                                    (outage_id, version, j(normalized), actor, now()))
            self.conn.execute("UPDATE outages SET state='assessing',revision=revision+1,updated_at=? WHERE id=?", (now(), outage_id))
            self.store.audit(actor, "plan.create", "plan", cur.lastrowid, {"outage_id": outage_id, "version": version, "steps": len(normalized)})
        return self._plan_dict(self._row("plans", cur.lastrowid))

    def submit_plan(self, actor: str | None, role: str | None, plan_id: int, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"dispatcher"}); plan = self._row("plans", plan_id)
        if plan["state"] != "draft": raise ApiError(409, "只有草稿计划可以提交")
        self._plan_update(plan, "submitted", expected_revision, actor, "plan.submit", {})
        return self._plan_dict(self._row("plans", plan_id))

    def approve_plan(self, actor: str | None, role: str | None, plan_id: int, expected_revision: int, note: str = "") -> dict:
        actor = self._actor(actor, role, {"dispatcher"}); plan = self._row("plans", plan_id)
        if plan["state"] != "submitted": raise ApiError(409, "只有已提交计划可以批准")
        self._validate_safety(plan)
        self._plan_update(plan, "approved", expected_revision, actor, "plan.approve", {"note": note})
        self.conn.execute("UPDATE plans SET approved_by=?,approved_at=? WHERE id=?", (actor, now(), plan_id))
        self.conn.commit()
        return self._plan_dict(self._row("plans", plan_id))

    def activate_plan(self, actor: str | None, role: str | None, plan_id: int, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"dispatcher"}); plan = self._row("plans", plan_id)
        if plan["state"] != "approved": raise ApiError(409, "计划尚未批准")
        with self.conn:
            self.conn.execute("UPDATE plans SET state='superseded' WHERE outage_id=? AND state='active'", (plan["outage_id"],))
            updated = self.conn.execute("UPDATE plans SET state='active',revision=revision+1,activated_at=? WHERE id=? AND revision=?",
                                        (now(), plan_id, expected_revision))
            if updated.rowcount != 1: raise ApiError(409, "计划版本冲突")
            self.conn.execute("UPDATE outages SET state='restoring',revision=revision+1,updated_at=? WHERE id=?", (now(), plan["outage_id"]))
            self.store.audit(actor, "plan.activate", "plan", plan_id, {"outage_id": plan["outage_id"], "version": plan["version"]})
        return self._plan_dict(self._row("plans", plan_id))

    def make_plan_change(self, actor: str | None, role: str | None, base_plan_id: int, steps: list[dict], expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"dispatcher"}); base = self._row("plans", base_plan_id)
        if base["state"] not in {"approved", "active"}: raise ApiError(409, "只有已批准或执行中的计划可以变更")
        if int(expected_revision) != int(base["revision"]): raise ApiError(409, "计划已被修改，请刷新版本")
        normalized = self._validate_steps(steps)
        confirmed = {row["step_no"]: row for row in self.conn.execute("SELECT * FROM confirmations WHERE plan_id=? ORDER BY step_no", (base_plan_id,))}
        base_steps = {int(step["seq"]): step for step in json.loads(base["steps_json"])}
        for seq in confirmed:
            if seq not in {int(step["seq"]) for step in normalized} or normalized[[int(x["seq"]) for x in normalized].index(seq)] != base_steps[seq]:
                raise ApiError(409, "新计划不能改动已确认步骤")
        outage = self._row("outages", base["outage_id"])
        version = int(base["version"]) + 1
        with self.conn:
            cur = self.conn.execute("INSERT INTO plans(outage_id,version,state,steps_json,created_by,created_at) VALUES(?,?, 'draft',?,?,?)",
                                    (outage["id"], version, j(normalized), actor, now()))
            self.conn.execute("UPDATE plans SET state='superseded' WHERE id=?", (base_plan_id,))
            self._copy_confirmations(base_plan_id, cur.lastrowid, normalized, confirmed)
            self.store.audit(actor, "plan.change_create", "plan", cur.lastrowid, {"base_plan": base_plan_id, "version": version, "carried_confirmations": len(confirmed)})
        return self._plan_dict(self._row("plans", cur.lastrowid))

    def field_report(self, actor: str | None, role: str | None, plan_id: int, step_no: int, client_report_id: str, expected_plan_version: int, status: str, note: str = "") -> dict:
        actor = self._actor(actor, role, {"field"})
        if status not in {"started", "completed", "blocked"}: raise ApiError(400, "现场状态不合法")
        plan = self._row("plans", plan_id); steps = {int(x["seq"]): x for x in json.loads(plan["steps_json"])}
        if step_no not in steps: raise ApiError(400, "计划中没有该步骤")
        duplicate = self.conn.execute("SELECT * FROM field_reports WHERE client_report_id=?", (client_report_id,)).fetchone()
        if duplicate: return dict(duplicate)
        merge_status, conflict = "merged", None
        if plan["state"] != "active": merge_status, conflict = "conflict", "计划尚未激活"
        elif int(expected_plan_version) != int(plan["version"]): merge_status, conflict = "conflict", "现场报告基于旧计划版本"
        elif self.conn.execute("SELECT id FROM confirmations WHERE plan_id=? AND step_no=?", (plan_id, step_no)).fetchone():
            merge_status, conflict = "protected", "已确认记录不能由普通现场报告覆盖"
        with self.conn:
            cur = self.conn.execute("""INSERT INTO field_reports(client_report_id,plan_id,step_no,expected_plan_version,status,note,merge_status,conflict_reason,reported_by,received_at)
                                     VALUES(?,?,?,?,?,?,?,?,?,?)""", (client_report_id, plan_id, step_no, expected_plan_version, status, note, merge_status, conflict, actor, now()))
            if merge_status == "merged" and status in {"completed", "blocked"}:
                self.store.audit(actor, "field_report.merged", "plan", plan_id, {"step_no": step_no, "status": status, "client_report_id": client_report_id})
            self.store.audit(actor, "field_report.received", "plan", plan_id, {"step_no": step_no, "merge_status": merge_status, "conflict": conflict})
        result = dict(self._row("field_reports", cur.lastrowid))
        # 保供步骤的现场报告只留缺口说明，不能替代备用电源检查
        if merge_status == "merged" and steps[step_no].get("facility_id"):
            gap = self._backup_gap(steps[step_no], plan_id, step_no, now())
            result["backup_gap"] = None if gap.valid else gap.reasons
        return result

    # ---------- 备用电源检查（检查记录层） ----------

    def register_backup_check(self, actor: str | None, role: str | None, plan_id: int, step_no: int,
                              checked_at: str, sustainable_minutes: int, backup_mw: float,
                              demand_mw: float | None = None, note: str = "") -> dict:
        """调度员登记一次备用电源检查：检查时刻、可持续时长、截止时间、备用容量和用电需求。

        每次登记产生新版本记录；新版本会使该步骤上依据旧检查的确认失效（在判定层动态核验）。
        """
        actor = self._actor(actor, role, {"dispatcher"})
        step, plan = self._facility_step(plan_id, step_no)
        try:
            deadline = backup_rules.compute_deadline(checked_at, int(sustainable_minutes))
        except ValueError as exc: raise ApiError(400, str(exc)) from exc
        if float(backup_mw) < 0: raise ApiError(400, "备用可用容量不能为负")
        demand = float(demand_mw) if demand_mw is not None else float(step["required_mw"])
        if demand < 0: raise ApiError(400, "用电需求不能为负")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO backup_checks(plan_id,step_no,revision_kind,checked_at,sustainable_minutes,
                                     deadline,backup_mw,demand_mw,checked_by,created_at,note)
                                     VALUES(?,?, 'check',?,?,?,?,?,?,?,?)""",
                                    (plan_id, step_no, checked_at, int(sustainable_minutes), deadline,
                                     float(backup_mw), demand, actor, now(), note))
            self.store.audit(actor, "backup_check.register", "plan", plan_id,
                             {"step_no": step_no, "facility_id": step["facility_id"], "deadline": deadline,
                              "backup_mw": backup_mw, "demand_mw": demand, "backup_check_id": cur.lastrowid})
        record = dict(self._row("backup_checks", cur.lastrowid))
        record["verdict"] = backup_rules.evaluate(deadline=deadline, backup_mw=backup_mw,
                                                  demand_mw=demand, at=now()).as_dict()
        return record

    def update_backup_demand(self, actor: str | None, role: str | None, plan_id: int, step_no: int,
                             demand_mw: float, note: str = "") -> dict:
        """用电需求更新：沿用最近一次检查的容量与截止时间，追加新版本记录。

        需求变化后原确认失效，需要重新核验。
        """
        actor = self._actor(actor, role, {"dispatcher"})
        step, plan = self._facility_step(plan_id, step_no)
        latest = self._latest_check(plan_id, step_no)
        if not latest: raise ApiError(409, "该步骤尚未登记备用电源检查，无法单独更新用电需求")
        if float(demand_mw) < 0: raise ApiError(400, "用电需求不能为负")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO backup_checks(plan_id,step_no,revision_kind,checked_at,sustainable_minutes,
                                     deadline,backup_mw,demand_mw,checked_by,created_at,note)
                                     VALUES(?,?, 'demand',?,?,?,?,?,?,?,?)""",
                                    (plan_id, step_no, latest["checked_at"], latest["sustainable_minutes"],
                                     latest["deadline"], latest["backup_mw"], float(demand_mw), actor, now(), note))
            self.store.audit(actor, "backup_check.demand_update", "plan", plan_id,
                             {"step_no": step_no, "facility_id": step["facility_id"],
                              "demand_mw": demand_mw, "old_demand_mw": latest["demand_mw"],
                              "backup_check_id": cur.lastrowid})
        record = dict(self._row("backup_checks", cur.lastrowid))
        record["verdict"] = backup_rules.evaluate(deadline=latest["deadline"], backup_mw=latest["backup_mw"],
                                                  demand_mw=float(demand_mw), at=now()).as_dict()
        return record

    def _facility_step(self, plan_id: int, step_no: int) -> tuple[dict, sqlite3.Row]:
        plan = self._row("plans", plan_id)
        steps = {int(x["seq"]): x for x in json.loads(plan["steps_json"])}
        if step_no not in steps: raise ApiError(400, "计划中没有该步骤")
        step = steps[step_no]
        if not step.get("facility_id"): raise ApiError(400, "该步骤不是重要用户保供步骤，无需备用电源检查")
        return step, plan

    def _latest_check(self, plan_id: int, step_no: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM backup_checks WHERE plan_id=? AND step_no=? ORDER BY id DESC LIMIT 1",
                                 (plan_id, step_no)).fetchone()

    def _backup_gap(self, step: dict, plan_id: int, step_no: int, at: str) -> backup_rules.BackupVerdict:
        """判定层：返回某保供步骤当前的备用电源缺口判定。"""
        if not step.get("facility_id"):
            return backup_rules.BackupVerdict(STATE_OK, True, reasons=[])
        latest = self._latest_check(plan_id, step_no)
        if not latest:
            return backup_rules.missing()
        return backup_rules.evaluate(deadline=latest["deadline"], backup_mw=latest["backup_mw"],
                                     demand_mw=latest["demand_mw"], at=at)

    def _confirmation_effect(self, row: sqlite3.Row, steps: dict[int, dict], at: str) -> tuple[bool, str | None]:
        """判定层：确认是否仍然有效。检查结果或用电需求更新后，原确认失效需要重新核验。"""
        if row["status"] != "confirmed" or row["backup_check_id"] is None:
            return True, None
        step_no = int(row["step_no"]); current = self._latest_check(row["plan_id"], step_no)
        if current is None:
            return False, "备用电源检查记录已不存在"
        if int(current["id"]) != int(row["backup_check_id"]):
            label = "检查结果" if current["revision_kind"] == "check" else "用电需求"
            return False, f"{label}已更新，原确认失效，需要重新核验"
        verdict = backup_rules.evaluate(deadline=current["deadline"], backup_mw=current["backup_mw"],
                                        demand_mw=current["demand_mw"], at=at)
        if not verdict.valid:
            return False, verdict.reasons[0]
        return True, None

    def confirm_step(self, actor: str | None, role: str | None, plan_id: int, step_no: int, decision: str, note: str = "") -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        if decision not in {"confirmed", "blocked"}: raise ApiError(400, "确认状态不合法")
        plan = self._row("plans", plan_id)
        if plan["state"] != "active": raise ApiError(409, "只有执行中的计划可以确认")
        steps = {int(x["seq"]): x for x in json.loads(plan["steps_json"])}
        if step_no not in steps: raise ApiError(400, "计划中没有该步骤")
        report = self.conn.execute("SELECT * FROM field_reports WHERE plan_id=? AND step_no=? AND merge_status='merged' ORDER BY id DESC LIMIT 1", (plan_id, step_no)).fetchone()
        if not report: raise ApiError(409, "没有可确认的现场报告")
        if decision == "confirmed" and report["status"] != "completed": raise ApiError(409, "现场步骤尚未完成")
        for dependency in steps[step_no].get("depends_on", []):
            found = self.conn.execute("SELECT * FROM confirmations WHERE plan_id=? AND step_no=? AND status='confirmed'", (plan_id, int(dependency))).fetchone()
            if not found: raise ApiError(409, f"前置步骤 {dependency} 尚未确认")
        # 保供步骤必须有有效且余量充足的备用电源检查；现场报告不能替代检查
        gap = self._backup_gap(steps[step_no], plan_id, step_no, now())
        latest_check = self._latest_check(plan_id, step_no) if steps[step_no].get("facility_id") else None
        if decision == "confirmed" and not gap.valid:
            raise ApiError(409, "；".join(gap.reasons))
        with self.conn:
            self.conn.execute("""INSERT INTO confirmations(plan_id,step_no,status,confirmed_by,confirmed_at,note,backup_check_id,demand_mw_snapshot) VALUES(?,?,?,?,?,?,?,?)
                               ON CONFLICT(plan_id,step_no) DO UPDATE SET status=excluded.status,confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at,note=excluded.note,backup_check_id=excluded.backup_check_id,demand_mw_snapshot=excluded.demand_mw_snapshot""",
                              (plan_id, step_no, decision, actor, now(), note,
                               latest_check["id"] if latest_check else None,
                               float(latest_check["demand_mw"]) if latest_check else None))
            self.store.audit(actor, "plan.confirm_step", "plan", plan_id,
                            {"step_no": step_no, "status": decision, "note": note,
                             "backup_gap": None if gap.valid else gap.reasons})
        return dict(self.conn.execute("SELECT * FROM confirmations WHERE plan_id=? AND step_no=?", (plan_id, step_no)).fetchone())

    def publish_status(self, actor: str | None, role: str | None, outage_id: int, plan_id: int) -> dict:
        actor = self._actor(actor, role, {"dispatcher"})
        plan = self._row("plans", plan_id); outage = self._row("outages", outage_id)
        if plan["outage_id"] != outage_id: raise ApiError(400, "计划不属于该事故")
        confirmations = {row["step_no"]: dict(row) for row in self.conn.execute("SELECT * FROM confirmations WHERE plan_id=?", (plan_id,))}
        steps = json.loads(plan["steps_json"]); steps_by_seq = {int(s["seq"]): s for s in steps}
        at = now()
        blocked: list[str] = []

        def confirmed_effective(seq: int) -> bool:
            row = confirmations.get(seq)
            if not row or row.get("status") != "confirmed": return False
            row = self.conn.execute("SELECT * FROM confirmations WHERE plan_id=? AND step_no=?", (plan_id, seq)).fetchone()
            effective, _ = self._confirmation_effect(row, steps_by_seq, at)
            return effective

        completed = sum(1 for step in steps if confirmed_effective(int(step["seq"])))
        all_done = completed == len(steps)

        # 发布恢复完成前：受影响区域的每个重要用户都要有有效检查
        if all_done:
            affected = set(json.loads(outage["affected_regions_json"]))
            facilities = [dict(r) for r in self.conn.execute("SELECT * FROM facilities WHERE connected=1 ORDER BY id")]
            covered_facilities: set[int] = set()
            for step in steps:
                fid = step.get("facility_id")
                if fid: covered_facilities.add(int(fid))
            for facility in facilities:
                asset = self._row("assets", facility["asset_id"])
                if asset["region"] not in affected: continue
                fid = int(facility["id"])
                if fid not in covered_facilities:
                    blocked.append(f"受影响重要用户「{facility['name']}」没有对应的保供步骤和备用电源检查")
                    continue
                step_no = next(int(s["seq"]) for s in steps if int(s["facility_id"]) == fid)
                gap = self._backup_gap(steps_by_seq[step_no], plan_id, step_no, at)
                if not gap.valid:
                    blocked.extend([f"重要用户「{facility['name']}」步骤 {step_no}：{r}" for r in gap.reasons])
            if blocked:
                raise ApiError(409, "恢复完成发布被阻止：" + "；".join(blocked))

        status = {"outage_id": outage_id, "incident_code": outage["incident_code"], "plan_id": plan_id, "plan_version": plan["version"],
                  "state": "restored" if all_done else "restoring", "completed_steps": completed, "total_steps": len(steps),
                  "critical_blocked": [x for x in confirmations.values() if x["status"] == "blocked"],
                  "backup_blocked": blocked}
        with self.conn:
            cur = self.conn.execute("INSERT INTO published_status(outage_id,plan_id,version,status_json,created_at) VALUES(?,?,?,?,?)",
                                    (outage_id, plan_id, plan["version"], j(status), now()))
            if status["state"] == "restored": self.conn.execute("UPDATE outages SET state='restored',revision=revision+1,updated_at=? WHERE id=?", (now(), outage_id))
            self.store.audit(actor, "status.publish", "outage", outage_id, {"plan_id": plan_id, "state": status["state"]})
        return {"id": cur.lastrowid, "status": status}

    def _validate_steps(self, steps: list[dict]) -> list[dict]:
        if not steps: raise ApiError(400, "恢复计划至少需要一个步骤")
        normalized = []; seqs = set()
        for raw in steps:
            try:
                seq, asset_code, required = int(raw["seq"]), str(raw["asset"]), float(raw.get("required_mw", 0))
            except (KeyError, ValueError, TypeError) as exc: raise ApiError(400, "步骤字段不完整") from exc
            if seq in seqs or required <= 0: raise ApiError(400, "步骤序号重复或容量不合法")
            asset = self.conn.execute("SELECT * FROM assets WHERE code=?", (asset_code,)).fetchone()
            if not asset: raise ApiError(400, f"步骤资产不存在：{asset_code}")
            if required > float(asset["capacity_mw"]): raise ApiError(409, f"步骤 {seq} 超过资产安全容量")
            facility_id = raw.get("facility_id")
            if facility_id is not None:
                facility_id = int(facility_id)
                facility = self.conn.execute("SELECT * FROM facilities WHERE id=?", (facility_id,)).fetchone()
                if not facility: raise ApiError(400, f"步骤 {seq} 关联的重要用户不存在")
                if int(facility["asset_id"]) != int(asset["id"]): raise ApiError(400, f"步骤 {seq} 保供的重要用户不属于步骤资产")
            deps = [int(x) for x in raw.get("depends_on", [])]
            if any(dep >= seq for dep in deps): raise ApiError(400, "依赖步骤必须位于当前步骤之前")
            seqs.add(seq); normalized.append({"seq": seq, "action": str(raw.get("action", "energize")), "asset": asset_code,
                                                  "required_mw": required, "depends_on": deps, "critical": bool(raw.get("critical", False)),
                                                  "facility_id": facility_id})
        available = {row["seq"]: set(row["depends_on"]) for row in normalized}
        for seq, deps in available.items():
            if not deps.issubset(seqs): raise ApiError(400, f"步骤 {seq} 含有未知依赖")
        return sorted(normalized, key=lambda x: x["seq"])

    def _validate_safety(self, plan: sqlite3.Row) -> None:
        for step in json.loads(plan["steps_json"]):
            for dependency in step.get("depends_on", []):
                if int(dependency) >= int(step["seq"]): raise ApiError(409, "计划依赖顺序不安全")

    def _copy_confirmations(self, old_plan_id: int, new_plan_id: int, steps: list[dict], confirmed: dict[int, sqlite3.Row]) -> None:
        for step in steps:
            seq = int(step["seq"])
            if seq in confirmed:
                old = confirmed[seq]
                new_check_id = self._copy_latest_backup_check(old_plan_id, new_plan_id, seq)
                self.conn.execute("""INSERT INTO confirmations(plan_id,step_no,status,confirmed_by,confirmed_at,note,backup_check_id,demand_mw_snapshot)
                                     VALUES(?,?,?,?,?,?,?,?)""",
                                  (new_plan_id, seq, old["status"], old["confirmed_by"], old["confirmed_at"], old["note"],
                                   new_check_id, old["demand_mw_snapshot"]))

    def _copy_latest_backup_check(self, old_plan_id: int, new_plan_id: int, step_no: int) -> int | None:
        latest = self._latest_check(old_plan_id, step_no)
        if not latest:
            return None
        cur = self.conn.execute("""INSERT INTO backup_checks(plan_id,step_no,revision_kind,checked_at,sustainable_minutes,
                                 deadline,backup_mw,demand_mw,checked_by,created_at,note)
                                 VALUES(?,?, 'check',?,?,?,?,?,?,?,?)""",
                                (new_plan_id, step_no, latest["checked_at"], latest["sustainable_minutes"],
                                 latest["deadline"], latest["backup_mw"], latest["demand_mw"],
                                 latest["checked_by"], now(), latest["note"]))
        return int(cur.lastrowid)

    def _plan_update(self, plan: sqlite3.Row, state: str, expected_revision: int, actor: str, action: str, details: dict) -> None:
        if int(expected_revision) != int(plan["revision"]): raise ApiError(409, "计划版本冲突")
        with self.conn:
            cur = self.conn.execute("UPDATE plans SET state=?,revision=revision+1 WHERE id=? AND revision=?", (state, plan["id"], expected_revision))
            if cur.rowcount != 1: raise ApiError(409, "并发计划更新冲突")
            self.store.audit(actor, action, "plan", plan["id"], details)

    def plan_detail(self, plan_id: int) -> dict:
        plan = self._plan_dict(self._row("plans", plan_id))
        at = now()
        steps_by_seq = {int(step["seq"]): step for step in plan["steps"]}
        confirmations = [dict(row) for row in self.conn.execute("SELECT * FROM confirmations WHERE plan_id=? ORDER BY step_no", (plan_id,))]
        reports = [dict(row) for row in self.conn.execute("SELECT * FROM field_reports WHERE plan_id=? ORDER BY id", (plan_id,))]
        confirmation_by_step = {int(row["step_no"]): row for row in
                                self.conn.execute("SELECT * FROM confirmations WHERE plan_id=?", (plan_id,))}

        # 计划页层：把检查记录经判定层换算为每步余量和卡住原因
        step_pages = []
        for step in plan["steps"]:
            seq = int(step["seq"])
            gap = self._backup_gap(step, plan_id, seq, at)
            latest = self._latest_check(plan_id, seq) if step.get("facility_id") else None
            stuck: list[str] = []
            if step.get("facility_id"):
                if not gap.valid:
                    stuck.extend(gap.reasons)
                confirmation = confirmation_by_step.get(seq)
                if confirmation is not None:
                    effective, stale_reason = self._confirmation_effect(confirmation, steps_by_seq, at)
                    if not effective:
                        stuck.append(f"原确认已失效：{stale_reason}")
            step_pages.append({"seq": seq, "facility_id": step.get("facility_id"),
                               "required_mw": step["required_mw"], "backup": gap.as_dict(),
                               "backup_check_id": latest["id"] if latest else None,
                               "stuck_reasons": stuck, "can_confirm": not stuck})

        for row in confirmations:
            if row["backup_check_id"] is not None:
                effective, stale_reason = self._confirmation_effect(
                    self.conn.execute("SELECT * FROM confirmations WHERE id=?", (row["id"],)).fetchone(), steps_by_seq, at)
                row["effective"] = effective
                row["stale_reason"] = stale_reason
            else:
                row["effective"] = True
                row["stale_reason"] = None
        for report in reports:
            step = steps_by_seq.get(int(report["step_no"]))
            if step and step.get("facility_id") and report["merge_status"] == "merged":
                gap = self._backup_gap(step, plan_id, int(report["step_no"]), at)
                report["backup_gap"] = None if gap.valid else gap.reasons

        return {"plan": plan, "confirmations": confirmations, "field_reports": reports, "step_pages": step_pages}

    def _outage_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "incident_code": row["incident_code"], "title": row["title"], "state": row["state"],
                "affected_regions": json.loads(row["affected_regions_json"]), "revision": row["revision"]}

    def _plan_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "outage_id": row["outage_id"], "version": row["version"], "state": row["state"],
                "steps": json.loads(row["steps_json"]), "revision": row["revision"]}

    def state(self) -> dict:
        return {"assets": [dict(row) for row in self.conn.execute("SELECT * FROM assets ORDER BY id")],
                "facilities": [dict(row) for row in self.conn.execute("SELECT * FROM facilities ORDER BY priority,id")],
                "outages": [self._outage_dict(row) for row in self.conn.execute("SELECT * FROM outages ORDER BY id DESC")],
                "plans": [self._plan_dict(row) for row in self.conn.execute("SELECT * FROM plans ORDER BY id DESC")],
                "telemetry_anomalies": [dict(row) for row in self.conn.execute("SELECT * FROM telemetry WHERE valid=0 ORDER BY id DESC LIMIT 20")],
                "audits": [dict(row) for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")]}

    def seed(self) -> None:
        if not self.conn.execute("SELECT id FROM assets LIMIT 1").fetchone():
            a = self.register_asset("dispatcher-demo", "dispatcher", "SUB-1", "中心站", "substation", 200, "城区")
            self.register_asset("dispatcher-demo", "dispatcher", "LINE-1", "一号线", "line", 120, "城区", a["id"])
            self.register_facility("dispatcher-demo", "dispatcher", "市医院", "hospital", a["id"], 1, 50)


class Handler(BaseHTTPRequestHandler):
    service: GridService

    def log_message(self, fmt: str, *args: object) -> None: sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try: return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc: raise ApiError(400, "JSON 请求体无效") from exc
    def _parts(self) -> list[str]: return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self) -> None:
        try:
            p = self._parts()
            if p in (["health"], ["api", "health"]): out = {"status": "ok"}
            elif p == ["api", "state"]: out = self.service.state()
            elif len(p) == 3 and p[:2] == ["api", "plans"]: out = self.service.plan_detail(int(p[2]))
            elif not p:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except Exception as exc: self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, b = self._parts(), self._body(); actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p == ["api", "assets"]: out = self.service.register_asset(actor, role, b.get("code", ""), b.get("name", ""), b.get("asset_type", "line"), float(b.get("capacity_mw", 0)), b.get("region", ""), b.get("parent_id"))
            elif p == ["api", "facilities"]: out = self.service.register_facility(actor, role, b.get("name", ""), b.get("facility_type", "hospital"), int(b.get("asset_id", 0)), int(b.get("priority", 1)), float(b.get("backup_power_mw", 0)))
            elif p == ["api", "outages"]: out = self.service.create_outage(actor, role, b.get("incident_code", ""), b.get("title", ""), b.get("affected_regions", []))
            elif p == ["api", "telemetry"]: out = self.service.record_telemetry(actor, role, int(b.get("asset_id", 0)), float(b.get("load_mw", 0)), float(b.get("voltage_kv", 0)), b.get("timestamp", ""))
            elif p == ["api", "plans"]: out = self.service.create_plan(actor, role, int(b.get("outage_id", 0)), b.get("steps", []))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "submit": out = self.service.submit_plan(actor, role, int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "approve": out = self.service.approve_plan(actor, role, int(p[2]), int(b.get("expected_revision", -1)), b.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "activate": out = self.service.activate_plan(actor, role, int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "change": out = self.service.make_plan_change(actor, role, int(p[2]), b.get("steps", []), int(b.get("expected_revision", -1)))
            elif p == ["api", "field-reports"]: out = self.service.field_report(actor, role, int(b.get("plan_id", 0)), int(b.get("step_no", 0)), b.get("client_report_id", ""), int(b.get("expected_plan_version", 0)), b.get("status", ""), b.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "confirm": out = self.service.confirm_step(actor, role, int(p[2]), int(b.get("step_no", 0)), b.get("decision", "confirmed"), b.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "backup-checks": out = self.service.register_backup_check(actor, role, int(p[2]), int(b.get("step_no", 0)), b.get("checked_at", ""), int(b.get("sustainable_minutes", 0)), float(b.get("backup_mw", 0)), b.get("demand_mw"), b.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "plans"] and p[3] == "backup-demand": out = self.service.update_backup_demand(actor, role, int(p[2]), int(b.get("step_no", 0)), float(b.get("demand_mw", 0)), b.get("note", ""))
            elif p == ["api", "status"]: out = self.service.publish_status(actor, role, int(b.get("outage_id", 0)), int(b.get("plan_id", 0)))
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc: self._send(400, {"error": str(exc)})
        except Exception as exc: self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path); service = GridService(store)
    if seed: service.seed()
    Handler.service = service
    print(f"grid restoration listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8215); parser.add_argument("--db", default=str(DB_PATH)); parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init: Store(args.db).close()
    if args.seed or not args.init: run(args.port, args.db, args.seed)


if __name__ == "__main__": main()
