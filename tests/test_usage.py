"""v0.2 方案 §8 第 1、2 条：用量缓存解析与 `usage` 命令。"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import usage  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
UUID = "11111111-2222-3333-4444-555555555555"


def cache_doc(fetched_at=NOW - timedelta(minutes=10), five=23, seven=41.5,
              five_reset=NOW + timedelta(hours=2), seven_reset=NOW + timedelta(days=3),
              cache_uuid=UUID, account_uuid=UUID, extra=None):
    utilization = {}
    if five is not None:
        utilization["five_hour"] = {"utilization": five,
                                    "resets_at": five_reset.isoformat() if five_reset else None}
    if seven is not None:
        utilization["seven_day"] = {"utilization": seven,
                                    "resets_at": seven_reset.isoformat() if seven_reset else None}
    doc = {"cachedUsageUtilization": {"fetchedAtMs": int(fetched_at.timestamp() * 1000),
                                      "utilization": utilization},
           "oauthAccount": {"emailAddress": "secret-person@example.com"}}
    if cache_uuid is not None:
        doc["cachedUsageUtilization"]["accountUuid"] = cache_uuid
    if account_uuid is not None:
        doc["oauthAccount"]["accountUuid"] = account_uuid
    doc.update(extra or {})
    return doc


class ParseTest(CliTestCase):
    def path(self, doc):
        return self.write(os.path.join(self.tmp, "state.json"), json.dumps(doc) if not isinstance(doc, str) else doc)

    def test_ok_and_stale(self):
        report = usage.read_usage(self.path(cache_doc()), NOW)
        self.assertEqual(report.status, usage.STATUS_OK)
        self.assertEqual(report.five_hour.percent, 23)
        self.assertEqual(report.seven_day.percent, 41.5)
        self.assertEqual(report.five_hour.resets_at, NOW + timedelta(hours=2))
        stale = usage.read_usage(self.path(cache_doc(fetched_at=NOW - timedelta(hours=2))), NOW)
        self.assertEqual(stale.status, usage.STATUS_STALE)
        self.assertEqual(stale.five_hour.percent, 23)

    def test_missing_or_broken(self):
        self.assertEqual(usage.read_usage(os.path.join(self.tmp, "nope.json"), NOW).status, usage.STATUS_NO_DATA)
        self.assertEqual(usage.read_usage(self.path("{broken"), NOW).status, usage.STATUS_UNREADABLE)
        self.assertEqual(usage.read_usage(self.path([1, 2]), NOW).status, usage.STATUS_UNREADABLE)
        self.assertEqual(usage.read_usage(self.path({}), NOW).status, usage.STATUS_NO_DATA)
        bad_time = cache_doc()
        bad_time["cachedUsageUtilization"]["fetchedAtMs"] = "yesterday"
        self.assertEqual(usage.read_usage(self.path(bad_time), NOW).status, usage.STATUS_NO_DATA)
        bad_time["cachedUsageUtilization"]["fetchedAtMs"] = 10 ** 30
        self.assertEqual(usage.read_usage(self.path(bad_time), NOW).status, usage.STATUS_NO_DATA)
        bad_time["cachedUsageUtilization"]["fetchedAtMs"] = -10 ** 30
        self.assertEqual(usage.read_usage(self.path(bad_time), NOW).status, usage.STATUS_NO_DATA)
        self.assertEqual(usage.read_usage(self.path(cache_doc(five=None, seven=None)), NOW).status,
                         usage.STATUS_NO_DATA)

    def test_bad_window_values(self):
        doc = cache_doc()
        doc["cachedUsageUtilization"]["utilization"]["five_hour"]["utilization"] = "23%"
        doc["cachedUsageUtilization"]["utilization"]["seven_day"]["utilization"] = True
        self.assertEqual(usage.read_usage(self.path(doc), NOW).status, usage.STATUS_NO_DATA)
        doc = cache_doc(five_reset=None)
        doc["cachedUsageUtilization"]["utilization"]["seven_day"]["resets_at"] = "not a time"
        report = usage.read_usage(self.path(doc), NOW)
        self.assertIsNone(report.five_hour.resets_at)
        self.assertIsNone(report.seven_day.resets_at)

    def test_other_account(self):
        report = usage.read_usage(self.path(cache_doc(cache_uuid="other")), NOW)
        self.assertEqual(report.status, usage.STATUS_OTHER_ACCOUNT)
        self.assertIsNone(report.five_hour)
        # 任一侧缺失 accountUuid 时不做判断。
        self.assertEqual(usage.read_usage(self.path(cache_doc(cache_uuid=None)), NOW).status, usage.STATUS_OK)
        self.assertEqual(usage.read_usage(self.path(cache_doc(account_uuid=None)), NOW).status, usage.STATUS_OK)

    def test_reset_window(self):
        report = usage.read_usage(self.path(cache_doc(five_reset=NOW - timedelta(minutes=1))), NOW)
        self.assertTrue(usage.window_is_reset(report.five_hour, NOW))
        self.assertFalse(usage.window_is_reset(report.seven_day, NOW))
        self.assertTrue(usage.report_to_dict(report, NOW)["five_hour"]["reset"])


class UsageCommandTest(CliTestCase):
    def write_state(self, name, doc):
        self.write(os.path.join(self.root, name, ".claude.json"), json.dumps(doc))

    def test_table_and_json(self):
        self.ok("add", "work")
        self.ok("add", "idle")
        now = datetime.now(timezone.utc)
        self.write_state("work", cache_doc(fetched_at=now - timedelta(minutes=5),
                                           five_reset=now + timedelta(hours=1), seven_reset=now + timedelta(days=3)))
        result = self.ok("usage")
        self.assertRegex(result.out, r"work\s+23% \d\d:\d\d\s+42% (Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+5m ago")
        self.assertIn("no data (start claude-idle once)", result.out)
        self.assertIn("never reads credentials", result.out)
        self.assertNotIn("secret-person", result.out + result.err)
        data = json.loads(self.ok("usage", "work", "--json").out)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual([entry["name"] for entry in data["accounts"]], ["work"])
        self.assertEqual(data["accounts"][0]["usage"]["status"], "ok")
        self.assertEqual(data["accounts"][0]["usage"]["seven_day"]["percent"], 41.5)

    def test_stale_and_other_account_rows(self):
        self.ok("add", "old")
        self.ok("add", "moved")
        now = datetime.now(timezone.utc)
        self.write_state("old", cache_doc(fetched_at=now - timedelta(hours=45)))
        self.write_state("moved", cache_doc(cache_uuid="someone-else"))
        out = self.ok("usage").out
        self.assertRegex(out, r"old\s+.*45h ago \(stale\)")
        self.assertRegex(out, r"moved\s+-\s+-\s+cache belongs to an earlier login")

    def test_default_identity_reads_home_state(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "main")
        now = datetime.now(timezone.utc)
        self.write(os.path.join(self.home, ".claude.json"), json.dumps(cache_doc(fetched_at=now)))
        data = json.loads(self.ok("usage", "--json").out)
        self.assertEqual(data["accounts"][0]["usage"]["five_hour"]["percent"], 23)

    def test_unregistered_and_unconfigured(self):
        self.assertEqual(self.run_cli("usage", "nobody").code, 1)
        self.assertEqual(json.loads(self.ok("usage", "--json").out),
                         {"schema_version": 1, "configured": False, "accounts": []})


if __name__ == "__main__":
    unittest.main()
