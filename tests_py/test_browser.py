import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from portal.server import start
from playwright.sync_api import sync_playwright

from collector.browser import PortalSession, collect, Faults
from collector.errors import FilterWithdrawn, LoginExpired
from collector.provenance import verify_download

FILTERS_B = {'org': 'b', 'period': '2026-10', 'metric': 'gross'}
FILTERS_A = {'org': 'a', 'period': '2026-10', 'metric': 'net'}


class PortalBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_cascading_options_refresh_and_confirmed_selection(self):
        with sync_playwright() as playwright:
            with PortalSession(playwright, self.base) as session:
                session.open().login()
                org_a = session.select_org('a')
                self.assertEqual(org_a['confirmed'], 'a / 2026-10 / net')
                self.assertIn('2026-11', org_a['periodOptions'])
                switched = session.select_org('b')
                # 异步刷新完成后页面确认值必须已经是乙机构，不能残留甲机构状态。
                self.assertEqual(switched['org'], 'b')
                self.assertEqual(switched['confirmed'].split(' / ')[0], 'b')
                session.select_period('2026-10')
                session.select_metric('gross')
                confirmed = session.confirmed_filters()
                self.assertEqual(
                    confirmed['confirmed'],
                    'b / 2026-10 / gross')

    def test_full_collect_round_file_matches_this_round_identity(self):
        with sync_playwright() as playwright:
            outcome = collect(playwright, self.base, FILTERS_B)
        self.assertEqual(outcome['confirmed']['org'], 'b')
        verified = verify_download(outcome['rawBytes'], outcome['exportId'], FILTERS_B)
        self.assertEqual(verified['rowCount'], 1)
        self.assertEqual(outcome['filename'], f"{outcome['exportId']}.csv")

    def test_same_org_runs_use_isolated_sessions_and_cannot_share_exports(self):
        with sync_playwright() as playwright:
            first = collect(playwright, self.base, FILTERS_A)
            second = collect(playwright, self.base, FILTERS_A)
            self.assertNotEqual(first['sessionId'], second['sessionId'])
            self.assertNotEqual(first['exportId'], second['exportId'])
            # 新会话读取第一次运行的导出号必须被拒绝。
            with PortalSession(playwright, self.base) as probe:
                probe.open().login()
                self.assertEqual(probe.fetch_export_status(first['exportId']), 404)

    def test_late_old_download_is_identified_by_export_id(self):
        # 同一会话先制造一个延迟的旧导出，再立刻触发本轮导出；
        # 采集器按 data-export-id 精确匹配本轮条目。
        with sync_playwright() as playwright:
            with PortalSession(playwright, self.base,
                               faults=Faults(export_delay=6)) as session:
                session.open().login()
                session.select_org('b')
                session.select_period('2026-10')
                session.select_metric('gross')
                session.confirmed_filters()
                old_export = session.trigger_export()
                # 旧条目还在 pending 时再触发一轮，新条目必须被独立识别。
                new_export = session.trigger_export()
                self.assertNotEqual(old_export, new_export)
                _, _, raw = self._download(session, new_export)
                with self.assertRaises(Exception):
                    verify_download(raw, old_export, FILTERS_B)
                verified = verify_download(raw, new_export, FILTERS_B)
                self.assertEqual(verified['rows'][0]['export_id'], new_export)

    @staticmethod
    def _download(session, export_id):
        path, filename = session.download(export_id)
        raw = Path(path).read_bytes()
        return path, filename, raw

    def test_expired_session_stops_with_typed_error(self):
        with sync_playwright() as playwright:
            with PortalSession(playwright, self.base,
                               faults=Faults(expire_session=True)) as session:
                session.open()
                with self.assertRaises(LoginExpired) as caught:
                    session.login()
                self.assertEqual(caught.exception.kind, 'login_expired')

    def test_withdrawn_filter_returns_typed_422_error(self):
        with sync_playwright() as playwright:
            with PortalSession(playwright, self.base,
                               faults=Faults(withdraw_filter=True)) as session:
                session.open().login()
                with self.assertRaises(FilterWithdrawn) as caught:
                    session.select_org('b')
                self.assertEqual(caught.exception.kind, 'filter_withdrawn')

    def test_interrupted_round_leaves_no_download_and_recoverable_state(self):
        with sync_playwright() as playwright:
            with PortalSession(playwright, self.base,
                               faults=Faults(interrupt_before_download=True)) as session:
                session.open().login()
                session.select_org('b')
                export_id = session.trigger_export()
                with self.assertRaises(Exception) as caught:
                    session.download(export_id)
                self.assertIn('中断', caught.exception.message)
        # 中断轮所属会话已关闭，其导出号在新会话不可见。
        with sync_playwright() as playwright:
            with PortalSession(playwright, self.base) as probe:
                probe.open().login()
                self.assertEqual(probe.fetch_export_status(export_id), 404)


class RunbookIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = start()
        cls.tmp = Path(tempfile.mkdtemp(prefix='collector-it-'))
        cls.state_root = cls.tmp / 'state'
        cls.harbor_root = cls.tmp / 'vault'
        from collector.runbook import Runbook
        cls.runbook = Runbook(cls.state_root, cls.harbor_root, logger=lambda *_: None)
        cls.runbook.bootstrap()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_end_to_end_replay_quarantine_and_trace(self):
        first = self.runbook.run_once(self.base, FILTERS_B, run_id='it-first')
        self.assertEqual(first['state'], 'completed')
        self.assertFalse(first['replay'])
        # 重试：同一筛选，不重复导入。
        retry = self.runbook.run_once(self.base, FILTERS_B, run_id='it-retry')
        self.assertTrue(retry['replay'])
        self.assertEqual(retry['harborReference'], first['harborReference'])
        # 失败文件被隔离且解释落盘。调包模式：文件名/导出号真实，但文件内容属于别的轮次。
        foreign = {'exportId': 'f' * 32, 'filters': FILTERS_B, 'replace': False,
                   'csv': ','.join(['f' * 32, FILTERS_B['org'], FILTERS_B['period'],
                                    FILTERS_B['metric'], 'r1', '125.50'])}
        failed = self.runbook.run_once(self.base, FILTERS_B, run_id='it-foreign',
                                       faults={'fulfill_download': foreign})
        self.assertEqual(failed['error'], 'provenance_mismatch')
        self.assertTrue(Path(failed['quarantinedFile']).is_file())
        self.assertTrue((self.runbook.runs_dir / 'it-foreign' / 'failure.json').is_file())
        # 台账：每轮先记 started 再记终态，终态条目应为 3 条。
        terminal = [entry for entry in self.runbook.history()
                    if entry['state'] != 'started']
        self.assertEqual(len(terminal), 3)
        traced = self.runbook.trace(export_id=first['exportId'])
        self.assertEqual(traced['reference'], first['harborReference'])
        back = self.runbook.trace(import_id=first['importId'])
        self.assertEqual(back['exportId'], first['exportId'])
        record = self.runbook.find_run('it-first')
        self.assertEqual(record['exportId'], first['exportId'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
