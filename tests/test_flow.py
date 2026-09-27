import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"], "安全校核通过")
        return outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def check(self, facility_id, outage_id, sustain=120, checked_at=None):
        checked_at = checked_at or iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        return self.s.register_backup_check("dispatcher", "dispatcher", facility_id, outage_id, checked_at, sustain)

    def test_full_restore_offline_merge_duplicate_and_plan_change(self):
        outage, plan = self.plan()
        report = self.s.field_report("field", "field", plan["id"], 1, "client-1", plan["version"], "completed", "设备已检查")
        self.assertEqual("merged", report["merge_status"])
        self.check(self.hospital["id"], outage["id"])
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed", "现场照片核验")
        self.assertEqual("confirmed", confirmed["status"])
        protected = self.s.field_report("field", "field", plan["id"], 1, "client-1-protected", plan["version"], "blocked", "补充遥测")
        self.assertEqual("protected", protected["merge_status"])
        plan2 = self.s.make_plan_change("dispatcher", "dispatcher", plan["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80, "critical": True},
            {"seq": 2, "action": "送电", "asset": "LINE", "required_mw": 70, "depends_on": [1], "critical": True}], plan["revision"])
        detail = self.s.plan_detail(plan2["id"])
        self.assertEqual(1, len(detail["confirmations"]))
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

    def test_backup_check_gates_confirmation(self):
        outage, plan = self.plan("OUT-3")
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed", "设备已检查")
        with self.assertRaises(ApiError):  # 未登记检查
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        expired_at = iso(datetime.now(timezone.utc) - timedelta(hours=3))
        self.check(self.hospital["id"], outage["id"], sustain=60, checked_at=expired_at)
        with self.assertRaises(ApiError):  # 检查已失效
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.s.update_facility_demand("dispatcher", "dispatcher", self.hospital["id"], 60)
        self.check(self.hospital["id"], outage["id"])
        with self.assertRaises(ApiError):  # 余量不足：需求 60 > 备用 50
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        blocked = self.s.field_report("field", "field", plan["id"], 1, "c-2", plan["version"], "blocked", "备用电源缺口 10MW")
        self.assertEqual("merged", blocked["merge_status"])
        step1 = self.s.plan_detail(plan["id"])["steps"][0]
        self.assertTrue(any("余量不足" in reason for reason in step1["blocked_reasons"]))
        self.assertTrue(any("备用电源缺口 10MW" in reason for reason in step1["blocked_reasons"]))
        self.s.update_facility_demand("dispatcher", "dispatcher", self.hospital["id"], 30)
        self.s.field_report("field", "field", plan["id"], 1, "c-3", plan["version"], "completed", "缺口已消除")
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.assertEqual("confirmed", confirmed["status"])
        affected = self.s.plan_detail(plan["id"])["affected_facilities"][0]
        self.assertEqual(20, affected["margin_mw"])
        self.assertTrue(affected["ok"])

    def test_demand_or_check_update_invalidates_confirmation(self):
        outage, plan = self.plan("OUT-4")
        self.check(self.hospital["id"], outage["id"])
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        updated = self.check(self.hospital["id"], outage["id"])  # 检查结果更新
        self.assertEqual(1, updated["invalidated_confirmations"])
        confirmations = {c["step_no"]: c for c in self.s.plan_detail(plan["id"])["confirmations"]}
        self.assertEqual(0, confirmations[1]["valid"])
        self.s.field_report("field", "field", plan["id"], 2, "c-2", plan["version"], "completed")
        with self.assertRaises(ApiError):  # 前置确认已失效
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        merged = self.s.field_report("field", "field", plan["id"], 1, "c-3", plan["version"], "completed", "复核完成")
        self.assertEqual("merged", merged["merge_status"])  # 失效确认不再受保护
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")  # 重新核验
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        demand = self.s.update_facility_demand("dispatcher", "dispatcher", self.hospital["id"], 40)  # 用电需求更新
        self.assertEqual(1, demand["invalidated_confirmations"])
        confirmations = {c["step_no"]: c for c in self.s.plan_detail(plan["id"])["confirmations"]}
        self.assertEqual(0, confirmations[1]["valid"])
        self.assertEqual(1, confirmations[2]["valid"])  # 线路步骤不挂医院，不受影响

    def test_publish_restored_requires_valid_checks(self):
        outage = self.s.create_outage("dispatcher", "dispatcher", "OUT-5", "线路跳闸", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "送电", "asset": "LINE", "required_mw": 70, "critical": True}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        self.s.field_report("field", "field", plan["id"], 1, "c-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")  # LINE 上无重要用户
        self.assertFalse(self.s.outage_backup(outage["id"])["all_ok"])
        with self.assertRaises(ApiError):  # 医院无有效检查，不能发布恢复完成
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.check(self.hospital["id"], outage["id"])
        self.assertTrue(self.s.outage_backup(outage["id"])["all_ok"])
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])


if __name__ == "__main__": unittest.main()
