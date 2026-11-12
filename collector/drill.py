"""首版端到端演练：覆盖候选项刷新、旧下载晚到、中断后再执行与失败可查。

运行：
    python -I -m collector.drill --portal-url http://127.0.0.1:8787 \
        --state-root .demo-state/collector --harbor-root .demo-state/vault

演练全部使用合成门户与本地 DataHarbor，回环地址通信，无真实凭据。
"""

import argparse
import json
import sys
from pathlib import Path

# 允许以 `python -I collector/drill.py` 方式运行：-I 会忽略当前目录，
# 因此显式把仓库根目录加入 sys.path（指向代码目录，而非下载目录）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from portal.server import start  # noqa: E402

from collector.runbook import Runbook  # noqa: E402


class Reporter:
    def __init__(self):
        self.blocks = []

    def line(self, text=''):
        print(text)
        self.blocks.append(text)

    def section(self, title):
        self.line()
        self.line('=' * 72)
        self.line(title)
        self.line('=' * 72)

    def json(self, payload, keys):
        for key in keys:
            if key in payload:
                self.line(f'  - {key}: {json.dumps(payload[key], ensure_ascii=False)}')

    def save(self, path):
        Path(path).write_text('\n'.join(self.blocks) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='合作方门户采集端到端演练')
    parser.add_argument('--portal-url', help='不填则在随机回环端口启动内置演练门户')
    parser.add_argument('--state-root', default='.demo-state/collector')
    parser.add_argument('--harbor-root', default='.demo-state/vault')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--timeout-ms', type=int, default=20000)
    args = parser.parse_args()

    reporter = Reporter()
    reporter.line('合作方门户浏览器采集 × DataHarbor 入库 演练')
    reporter.line(f'运行目录 {args.state_root}；数据保险库 {args.harbor_root}')

    own_server = None
    base_url = args.portal_url
    if not base_url:
        own_server, base_url = start()
        reporter.line(f'内置演练门户已启动：{base_url}')
    try:
        runbook = Runbook(args.state_root, args.harbor_root, headed=args.headed,
                          timeout_ms=args.timeout_ms, logger=reporter.line)
        reporter.section('步骤 0：准备 DataHarbor 机构、数据集与固定来源契约')
        bootstrap = runbook.bootstrap()
        reporter.json(bootstrap, ['created', 'dataset', 'owner', 'steward'])
        contract = runbook._run_bridge('portal-contract', {})
        reporter.line(f"  - 固定列：{', '.join(contract['columns'])}")
        reporter.line(f"  - 质量门禁：{json.dumps(contract['gate'], ensure_ascii=False)}")

        # 场景一：甲机构默认期间/口径，验证登录后异步候选项刷新与首跑入库。
        reporter.section('场景一：甲机构首跑（候选项异步刷新，按页面实际确认值导出）')
        filters_a = {'org': 'a', 'period': '2026-10', 'metric': 'net'}
        run_a = runbook.run_once(base_url, filters_a, run_id='drill-a-first')
        reporter.json(run_a, ['state', 'sessionId', 'pageConfirmed', 'exportId',
                              'importId', 'harborReference', 'harborStatus', 'csvSha256'])
        assert run_a['state'] == 'completed' and run_a['replay'] is False

        # 场景二：切到乙机构 + 不同期间/口径，验证联动下拉的候选项刷新。
        reporter.section('场景二：切换乙机构与期间/口径（联动候选项延迟更新，拒绝位置点击）')
        filters_b = {'org': 'b', 'period': '2026-10', 'metric': 'gross'}
        run_b = runbook.run_once(base_url, filters_b, run_id='drill-b-switch')
        reporter.json(run_b, ['state', 'pageConfirmed', 'exportId', 'importId',
                              'harborReference', 'harborStatus'])
        assert run_b['state'] == 'completed'
        assert run_b['sessionId'] != run_a['sessionId'], '两次运行必须使用独立会话'

        # 场景三：同一会话内先触发一个故意延迟的旧导出，再触发本轮导出，
        # 验证下载条目按 export_id 精确匹配，迟到的旧下载不会被误收。
        reporter.section('场景三：旧导出晚到（同列表中精确匹配本轮导出号）')
        stale = runbook.run_once(base_url, filters_b, run_id='drill-b-slow',
                                 faults={'export_delay': 8})
        reporter.json(stale, ['state', 'exportId', 'pageConfirmed', 'harborReference', 'replay'])
        assert stale['state'] == 'completed'
        # 同筛选重跑：importId 相同，Harbor 必须重放而不是再建版本。
        assert stale['replay'] is True and stale['harborReference'] == run_b['harborReference']

        # 场景四：跨会话的旧导出号在新会话中必须 404，证明服务端会话隔离。
        reporter.section('场景四：独立会话无法读取上一轮导出（服务端会话隔离）')
        from playwright.sync_api import sync_playwright
        from collector.browser import PortalSession
        with sync_playwright() as playwright:
            with PortalSession(playwright, base_url, timeout_ms=args.timeout_ms) as probe:
                probe.open().login()
                status_for_a = probe.fetch_export_status(run_a['exportId'])
                status_for_b = probe.fetch_export_status(run_b['exportId'])
        reporter.line(f'  - 新会话读取旧导出号 {run_a["exportId"][:8]}… -> HTTP {status_for_a}')
        reporter.line(f'  - 新会话读取旧导出号 {run_b["exportId"][:8]}… -> HTTP {status_for_b}')
        assert status_for_a == 404 and status_for_b == 404

        # 场景五：下载被调包（文件名与导出号真实，但文件内部身份属于别的轮次），
        # 来源校验拦截并隔离文件——列结构相同也无法蒙混。
        reporter.section('场景五：下载文件身份不符（列结构相同但内部导出号属于别的轮次）')
        foreign = {'exportId': 'f' * 32,
                   'filters': filters_b,
                   'replace': False,
                   'csv': ','.join(contract['columns']) + '\n' +
                          ','.join(['f' * 32, filters_b['org'], filters_b['period'],
                                    filters_b['metric'], 'r1', '125.50'])}
        tampered = runbook.run_once(base_url, filters_b, run_id='drill-b-foreign',
                                    faults={'fulfill_download': foreign})
        reporter.json(tampered, ['state', 'error', 'stage', 'message', 'quarantinedFile'])
        assert tampered['state'] == 'failed' and tampered['error'] == 'provenance_mismatch'
        assert Path(tampered['quarantinedFile']).is_file()

        # 场景六：导出已生成但下载前中断；再次执行时新会话拿不到旧导出，
        # 重跑成功且 Harbor 侧仍是重放，不产生重复版本。
        reporter.section('场景六：中断后再次执行（旧导出随会话失效，重试不重复导入）')
        interrupted = runbook.run_once(base_url, filters_b, run_id='drill-b-interrupted',
                                       faults={'interrupt_before_download': True})
        reporter.json(interrupted, ['state', 'error', 'stage', 'message'])
        assert interrupted['state'] == 'failed'
        with sync_playwright() as playwright:
            with PortalSession(playwright, base_url, timeout_ms=args.timeout_ms) as probe:
                probe.open().login()
                stale_status = probe.fetch_export_status(
                    interrupted['context'].get('exportId', ''))
        reporter.line(f"  - 中断轮的导出号在新会话中 -> HTTP {stale_status}")
        assert stale_status == 404
        resumed = runbook.run_once(base_url, filters_b, run_id='drill-b-resumed')
        reporter.json(resumed, ['state', 'exportId', 'importId', 'harborReference',
                                'replay', 'committed'])
        assert resumed['state'] == 'completed' and resumed['replay'] is True
        assert resumed['harborReference'] == run_b['harborReference']

        # 场景七：登录失效，停在结构化错误，不做任何入库。
        reporter.section('场景七：登录失效（页面可理解的中断位置，零入库）')
        expired = runbook.run_once(base_url, filters_a, run_id='drill-a-expired',
                                   faults={'expire_session': True})
        reporter.json(expired, ['state', 'error', 'stage', 'message'])
        assert expired['state'] == 'failed' and expired['error'] == 'login_expired'

        # 场景八：筛选项被服务端撤回（422），同样停在可理解的位置。
        reporter.section('场景八：筛选项撤回（服务端 422，不触发导出）')
        withdrawn = runbook.run_once(base_url, filters_a, run_id='drill-a-withdrawn',
                                     faults={'withdraw_filter': True})
        reporter.json(withdrawn, ['state', 'error', 'stage', 'message'])
        assert withdrawn['state'] == 'failed' and withdrawn['error'] == 'filter_withdrawn'

        # 场景九：质量门禁失败的文件可查：构造负金额下载，进入隔离版本。
        reporter.section('场景九：质量门禁失败（文件保留证据，版本标记 quarantined，解释可查）')
        bad_filters = {'org': 'a', 'period': '2026-11', 'metric': 'gross'}
        bad_csv = ','.join(contract['columns']) + '\n' + ','.join(
            ['e' * 32, bad_filters['org'], bad_filters['period'], bad_filters['metric'],
             'r1', '-7.00'])
        bad = runbook.run_once(base_url, bad_filters, run_id='drill-a-badgate',
                               faults={'fulfill_download': {'exportId': 'e' * 32,
                                                            'filters': bad_filters,
                                                            'csv': bad_csv}})
        # 来源校验通过（文件身份真实），失败发生在 Harbor 门禁：版本被隔离但保留证据。
        reporter.json(bad, ['state', 'exportId', 'importId', 'harborReference',
                            'harborStatus', 'replay', 'gate'])
        assert bad['state'] == 'quarantined' and bad['harborStatus'] == 'quarantined'
        assert bad['gate']['passed'] is False
        trace_bad = runbook.trace(import_id='portal-a-2026-11-gross')
        reporter.line(f"  - 隔离版本：{trace_bad['reference']} 状态={trace_bad['status']}")
        assert trace_bad['status'] == 'quarantined'
        # 隔离版本的拒绝行证据仍可通过 DataHarbor 查询（见 tests/portalbridge.test.js）。

        # 总览：从一次运行找到门户导出号与 DataHarbor 版本（重试运行经台账映射）。
        reporter.section('追溯总览：运行 → 门户导出号 → DataHarbor 版本（双向）')
        for run_id in ('drill-a-first', 'drill-b-switch', 'drill-b-resumed'):
            traced = runbook.trace_run(run_id)
            reporter.line(f"  - {run_id}: export={traced['exportId']} "
                          f"-> {traced['harborReference']} ({traced['harborStatus']})，"
                          f"replay={traced['replay']}，importId={traced['importId']}")
            assert traced['found']
        # 反向：用首跑的门户导出号直接命中版本（文件内导出号与版本一致）。
        first_record = runbook.find_run('drill-a-first')
        by_export = runbook.trace(export_id=first_record['exportId'])
        reporter.line(f"  - 反向追溯 export={first_record['exportId'][:12]}… "
                      f"-> {by_export['reference']} ({by_export['status']})")
        assert by_export['found'] and by_export['reference'] == 'partner_metrics:1'
        ledger = runbook.history()
        reporter.line(f'  - 台账共 {len(ledger)} 条（含 started 与终态），文件：{runbook.ledger}')
        reporter.line('')
        reporter.line('全部演练断言通过：候选项刷新、晚到区分、中断续跑、失败隔离与双向追溯均可见。')
    finally:
        if own_server is not None:
            own_server.shutdown()
            own_server.server_close()
    report_path = Path(args.state_root) / 'drill-report.txt'
    reporter.save(report_path)
    print(f'\n演练报告已写入 {report_path}')


if __name__ == '__main__':
    main()
