import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store
from backup import compute_deadline


def utc(minutes_from_now: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


class GridFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "线路", "line", 100, "A", self.sub["id"])
        self.hospital = self.s.register_facility("dispatcher", "dispatcher", "医院", "hospital", self.sub["id"], 1, 50)

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def plan(self, code="OUT-1"):
        outage = self.s.create_outage("dispatcher", "dispatcher", code, "线路跳闸", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True, "facility_id": self.hospital["id"]},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"], "安全校核通过")
        return outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def register_check(self, plan, step_no=1, sustainable=120, backup=100, demand=40):
        return self.s.register_backup_check("dispatcher", "dispatcher", plan["id"], step_no,
                                           utc(0), sustainable, backup, demand)

    def test_full_restore_offline_merge_duplicate_and_plan_change(self):
        outage, plan = self.plan()
        report = self.s.field_report("field", "field", plan["id"], 1, "client-1", plan["version"], "completed", "设备已检查")
        self.assertEqual("merged", report["merge_status"])
        # 未登记备用电源检查：现场报告只留缺口说明
        self.assertIsNotNone(report["backup_gap"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "现场照片核验")
        check = self.register_check(plan)
        self.assertEqual("ok", check["verdict"]["state"])
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "现场照片核验")
        self.assertEqual("confirmed", confirmed["status"])
        protected = self.s.field_report("field", "field", plan["id"], 1, "client-1-protected", plan["version"], "blocked", "补充遥测")
        self.assertEqual("protected", protected["merge_status"])
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True, "facility_id": self.hospital["id"]},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}], plan["revision"])
        detail = self.s.plan_detail(plan2["id"])
        self.assertEqual(1, len(detail["confirmations"]))
        # 计划变更后检查记录一并复制，确认仍绑定检查快照
        self.assertIsNotNone(detail["confirmations"][0]["backup_check_id"])
        plan2 = self.s.submit_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.approve_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        plan2 = self.s.activate_plan("dispatcher", "dispatcher", plan2["id"], plan2["revision"])
        self.s.field_report("field", "field", plan2["id"], 2, "client-2", plan2["version"], "completed", "已送电")
        self.s.confirm_step("dispatcher", "dispatcher", plan2["id"], 2, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan2["id"])
        self.assertEqual("restored", status["status"]["state"])

    def test_anomaly_stale_report_dependency_and_permissions(self):
        outage, plan = self.plan("OUT-2")
        anomaly = self.s.record_telemetry("operator", "operator", self.line["id"], 500, 220, "2026-09-24T00:00:00Z")
        self.assertFalse(anomaly["valid"])
        with self.assertRaises(ApiError):
            self.s.field_report("operator", "operator", plan["id"], 1, "bad-role", plan["version"], "completed")
        stale = self.s.field_report("field", "field", plan["id"], 2, "stale", plan["version"] - 1, "completed")
        self.assertEqual("conflict", stale["merge_status"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        with self.assertRaises(ApiError):
            self.s.create_plan("dispatcher", "dispatcher", outage["id"], [{"seq": 1, "action": "送电", "asset": "LINE", "required_mw": 101}])

    def test_backup_expired_and_insufficient_block_confirmation(self):
        _, plan = self.plan("OUT-3")
        self.s.field_report("field", "field", plan["id"], 1, "r1", plan["version"], "completed")
        # 检查已失效（可持续时长 0 分钟，截止时间早于当前）
        expired = self.s.register_backup_check("dispatcher", "dispatcher", plan["id"], 1,
                                               utc(-10), 1, 100, 80)
        self.assertEqual("expired", expired["verdict"]["state"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        # 重新登记有效但余量不足：备用 50 < 需求 80
        insufficient = self.register_check(plan, backup=50, demand=80)
        self.assertEqual("insufficient", insufficient["verdict"]["state"])
        self.assertEqual(-30, insufficient["verdict"]["margin_mw"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        # 缺口判定不允许确认，但调度员可登记"卡住"结论
        blocked = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "blocked", "备用缺口30MW")
        self.assertEqual("blocked", blocked["status"])

    def test_demand_update_invalidates_confirmation_and_page_shows_gap(self):
        _, plan = self.plan("OUT-4")
        self.s.field_report("field", "field", plan["id"], 1, "r2", plan["version"], "completed")
        self.register_check(plan, backup=100, demand=80)
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        detail = self.s.plan_detail(plan["id"])
        sp = next(x for x in detail["step_pages"] if x["seq"] == 1)
        self.assertTrue(sp["can_confirm"]); self.assertEqual([], sp["stuck_reasons"])
        # 用电需求更新：余量由 20MW 变为 -10MW，原确认失效
        updated = self.s.update_backup_demand("dispatcher", "dispatcher", plan["id"], 1, 110, "负荷突增")
        self.assertEqual("insufficient", updated["verdict"]["state"])
        detail = self.s.plan_detail(plan["id"])
        sp = next(x for x in detail["step_pages"] if x["seq"] == 1)
        self.assertFalse(sp["can_confirm"])
        self.assertTrue(any("原确认已失效" in r for r in sp["stuck_reasons"]))
        conf = next(c for c in detail["confirmations"] if c["step_no"] == 1)
        self.assertFalse(conf["effective"])
        self.assertIn("用电需求", conf["stale_reason"])
        # 重新登记检查补足余量后才能再次确认
        self.register_check(plan, backup=120, demand=110)
        again = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "已重新核验")
        self.assertEqual("confirmed", again["status"])

    def test_check_reregistration_invalidates_confirmation(self):
        _, plan = self.plan("OUT-5")
        self.s.field_report("field", "field", plan["id"], 1, "r3", plan["version"], "completed")
        self.register_check(plan, backup=90, demand=80)
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        # 重新登记检查（新结果余量不足）
        self.register_check(plan, backup=10, demand=80)
        detail = self.s.plan_detail(plan["id"])
        conf = next(c for c in detail["confirmations"] if c["step_no"] == 1)
        self.assertFalse(conf["effective"])
        self.assertIn("检查结果", conf["stale_reason"])

    def test_publish_blocked_until_all_affected_facilities_checked(self):
        outage = self.s.create_outage("dispatcher", "dispatcher", "OUT-6", "全站停电", ["A"])
        # 计划步骤没有关联重要用户 → 受影响用户缺少检查，发布恢复完成被阻止
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "送电", "asset": "LINE", "required_mw": 70}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        self.s.field_report("field", "field", plan["id"], 1, "r6", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        with self.assertRaises(ApiError) as ctx:
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertIn("医院", str(ctx.exception))

    def test_facility_must_match_step_asset(self):
        outage = self.s.create_outage("dispatcher", "dispatcher", "OUT-7", "跳闸", ["A"])
        with self.assertRaises(ApiError):
            self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
                {"seq": 1, "action": "保供", "asset": "LINE", "required_mw": 70, "facility_id": self.hospital["id"]}])

    def test_deadline_computation(self):
        deadline = compute_deadline("2026-09-27T10:00:00Z", 90)
        self.assertEqual("2026-09-27T11:30:00Z", deadline)
        with self.assertRaises(ValueError):
            compute_deadline("2026-09-27T10:00:00Z", 0)


if __name__ == "__main__": unittest.main()
