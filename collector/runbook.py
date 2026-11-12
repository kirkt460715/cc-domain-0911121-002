"""采集运行编排：浏览器操作 → 来源校验 → DataHarbor 入库 → 运行台账。

目录约定（下载文件是不可信输入，与代码目录严格分离）：
    <state-root>/
      runs/<run-id>/
        input.json            本轮请求的筛选与故障配置
        <export-id>.csv       浏览器实际接收的下载文件
        receive.json          传给 Harbor 桥接的输入
        result.json           运行结果（含门户导出号与 Harbor 版本号）
        failure.json          失败解释（仅失败时）
      quarantine/<run-id>-<export-id>.csv   来源校验失败、未入库的文件
      ledger.jsonl            每次运行追加一行，跨运行可追溯
"""

import json
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .browser import collect
from .errors import CollectorError, IngestionRejected
from .provenance import verify_download

BRIDGE_CLI = Path(__file__).resolve().parents[1] / 'src' / 'portal-cli.js'


def utc_stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


class Runbook:
    def __init__(self, state_root, harbor_root, *, node_bin='node', headed=False,
                 timeout_ms=20000, logger=print):
        self.state_root = Path(state_root)
        self.harbor_root = Path(harbor_root)
        self.runs_dir = self.state_root / 'runs'
        self.quarantine_dir = self.state_root / 'quarantine'
        self.ledger = self.state_root / 'ledger.jsonl'
        self.node_bin = node_bin
        self.headed = headed
        self.timeout_ms = timeout_ms
        self.logger = logger
        for directory in (self.runs_dir, self.quarantine_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.ledger.touch(exist_ok=True)

    # -- 台账 -------------------------------------------------------------

    def _append_ledger(self, entry):
        with open(self.ledger, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n')

    def _write_json(self, path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def history(self):
        entries = []
        if self.ledger.exists():
            for line in self.ledger.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    entries.append(json.loads(line))
        return entries

    def find_run(self, run_id):
        path = self.runs_dir / run_id / 'result.json'
        if not path.exists():
            failure = self.runs_dir / run_id / 'failure.json'
            if failure.exists():
                return json.loads(failure.read_text(encoding='utf-8'))
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    # -- Harbor 桥接调用 --------------------------------------------------

    def _run_bridge(self, command, input_payload, csv_path=None):
        with tempfile.TemporaryDirectory(prefix='portal-bridge-') as tmp:
            input_path = Path(tmp) / 'input.json'
            self._write_json(input_path, input_payload)
            argv = [self.node_bin, str(BRIDGE_CLI), command,
                    '--root', str(self.harbor_root), '--input', str(input_path)]
            if csv_path is not None:
                argv += ['--csv', str(csv_path)]
            # 工作目录保持在临时目录之外，避免不可信下载目录中的模块被解释器加载。
            completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            try:
                document = json.loads(completed.stderr.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                document = {'error': 'bridge_failed', 'message': completed.stderr.strip()}
            raise IngestionRejected('DataHarbor 拒绝了该文件', document)
        return json.loads(completed.stdout)

    def bootstrap(self):
        return self._run_bridge('portal-bootstrap', {})

    def trace(self, *, export_id=None, import_id=None):
        query = {'exportId': export_id} if export_id else {'importId': import_id}
        return self._run_bridge('portal-trace', query)

    def trace_run(self, run_id):
        """从一次运行找到门户导出号与 DataHarbor 版本。

        重试运行的导出号是门户新生成的，但版本按确定性 importId 复用；
        因此先查本地运行台账，再经 importId 解析到不可变版本。
        """
        record = self.find_run(run_id)
        if not record or 'importId' not in record:
            return {'runId': run_id, 'found': False, 'record': record}
        version = self.trace(import_id=record['importId'])
        return {
            'runId': run_id,
            'found': bool(version.get('found')),
            'exportId': record['exportId'],
            'importId': record['importId'],
            'harborReference': version.get('reference'),
            'harborStatus': version.get('status'),
            'replay': record.get('replay'),
            'storedExportId': version.get('exportId'),
            'csvPath': record.get('csvPath'),
            'csvSha256': record.get('csvSha256'),
        }

    # -- 单轮运行 ---------------------------------------------------------

    def run_once(self, base_url, filters, *, faults=None, run_id=None, actor='alice'):
        run_id = run_id or f"run-{utc_stamp()}-{filters['org']}-{filters['period']}-{filters['metric']}"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self._write_json(run_dir / 'input.json',
                         {'filters': filters, 'faults': faults or {}, 'actor': actor,
                          'baseUrl': base_url})
        started = time.monotonic()
        entry = {'runId': run_id, 'at': datetime.now(timezone.utc).isoformat(),
                 'filters': filters, 'state': 'started'}
        self._append_ledger(entry)
        self.logger(f"[{run_id}] 开始采集：机构={filters['org']} 期间={filters['period']} 口径={filters['metric']}")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                outcome = collect(playwright, base_url, filters, headed=self.headed,
                                  faults=_build_faults(faults), timeout_ms=self.timeout_ms)
            self.logger(f"[{run_id}] 浏览器确认筛选：{outcome['confirmed']['text']}；"
                        f"导出号 {outcome['exportId']} 已下载")
            csv_path = run_dir / f"{outcome['exportId']}.csv"
            csv_path.write_bytes(outcome['rawBytes'])
            # 入库前核对文件自身的导出标识与冻结筛选，迟到文件在此被拦下。
            provenance = verify_download(outcome['rawBytes'], outcome['exportId'], filters)
            self.logger(f"[{run_id}] 来源校验通过：{provenance['rowCount']} 行，sha256={provenance['sha256'][:12]}…")
            receive_input = {'exportId': outcome['exportId'], 'filters': filters, 'runId': run_id}
            self._write_json(run_dir / 'receive.json', receive_input)
            # 门户每轮都会生成新的 export_id，但同一组筛选对应同一个确定性
            # importId：重复采集是同一逻辑批次的重试，必须复用既有版本。
            import_id = f"portal-{filters['org']}-{filters['period']}-{filters['metric']}"
            existing = self.trace(import_id=import_id)
            if existing.get('found'):
                ingestion = {'importId': import_id, 'reference': existing['reference'],
                             'status': existing['status'], 'replay': True, 'committed': False,
                             'rows': existing['rows'], 'gate': None, 'report': None}
                self.logger(f"[{run_id}] 同筛选批次已存在，复用版本 {existing['reference']}（不重复导入）")
            else:
                try:
                    ingestion = self._run_bridge('portal-receive', receive_input, csv_path)
                except IngestionRejected as error:
                    # 门禁/校验失败：文件真实但未达到入库标准，保留隔离版本与解释。
                    return self._record_ingestion_failure(
                        run_dir, entry, error, run_id, filters, started,
                        outcome, csv_path, provenance, import_id)
            verdict = '重放既有版本（未重复导入）' if ingestion['replay'] else \
                f"新建版本 {ingestion['reference']}"
            self.logger(f"[{run_id}] DataHarbor：{verdict}，状态 {ingestion['status']}")
            result = {
                'runId': run_id,
                # quarantined 也是已创建的版本，只是未通过门禁；与失败终止区分开。
                'state': 'completed' if ingestion['status'] != 'quarantined' else 'quarantined',
                'sessionId': outcome['sessionId'],
                'pageConfirmed': outcome['confirmed'],
                'filters': filters,
                'exportId': outcome['exportId'],
                'csvPath': str(csv_path),
                'csvSha256': provenance['sha256'],
                'rowCount': provenance['rowCount'],
                'importId': ingestion['importId'],
                'harborReference': ingestion['reference'],
                'harborStatus': ingestion['status'],
                'replay': ingestion['replay'],
                'committed': ingestion['committed'],
                'gate': ingestion['gate'],
                'elapsedMs': round((time.monotonic() - started) * 1000),
            }
            self._write_json(run_dir / 'result.json', result)
            self._append_ledger({**entry, 'state': result['state'], 'exportId': result['exportId'],
                                 'importId': result['importId'],
                                 'harborReference': result['harborReference'],
                                 'replay': result['replay']})
            return result
        except CollectorError as error:
            return self._record_failure(run_dir, entry, error, run_id, filters, started)
        except Exception as error:  # 未预期错误同样落盘，避免静默丢失
            wrapped = CollectorError(str(error), {'type': type(error).__name__})
            wrapped.kind = 'unexpected'
            return self._record_failure(run_dir, entry, wrapped, run_id, filters, started)

    def _record_ingestion_failure(self, run_dir, entry, error, run_id, filters, started,
                                  outcome, csv_path, provenance, import_id):
        """来源真实但 Harbor 拒绝（如门禁失败）：版本被隔离，解释与文件可查。"""
        document = error.document()
        detail = document.get('context', {})
        document.update({'runId': run_id, 'state': 'ingestion_failed', 'filters': filters,
                         'sessionId': outcome['sessionId'], 'exportId': outcome['exportId'],
                         'importId': import_id, 'csvPath': str(csv_path),
                         'csvSha256': provenance['sha256'],
                         'elapsedMs': round((time.monotonic() - started) * 1000),
                         'harborError': detail})
        self._write_json(run_dir / 'failure.json', document)
        self._append_ledger({**entry, 'state': 'ingestion_failed', 'error': document['error'],
                             'message': document['message'], 'exportId': document['exportId']})
        self.logger(f"[{run_id}] 入库被拒（{document['stage']}）：{document['message']}；"
                    f"文件保留在 {csv_path}")
        return document

    def _record_failure(self, run_dir, entry, error, run_id, filters, started):
        document = error.document()
        document.update({'runId': run_id, 'state': 'failed', 'filters': filters,
                         'elapsedMs': round((time.monotonic() - started) * 1000)})
        # 若已下载文件但来源校验失败，把文件移入隔离区并在解释中给出路径。
        csv_candidates = sorted(run_dir.glob('*.csv'))
        if csv_candidates:
            target = self.quarantine_dir / f'{run_id}-{csv_candidates[0].name}'
            shutil.move(str(csv_candidates[0]), target)
            document['quarantinedFile'] = str(target)
        self._write_json(run_dir / 'failure.json', document)
        self._append_ledger({**entry, 'state': 'failed', 'error': document['error'],
                             'message': document['message'],
                             'quarantinedFile': document.get('quarantinedFile')})
        self.logger(f"[{run_id}] 失败停在 {document['stage']}：{document['message']}")
        return document


def _build_faults(config):
    from .browser import Faults
    config = config or {}
    return Faults(
        withdraw_filter=bool(config.get('withdraw_filter')),
        expire_session=bool(config.get('expire_session')),
        export_delay=config.get('export_delay'),
        fulfill_download=config.get('fulfill_download'),
        interrupt_before_download=bool(config.get('interrupt_before_download')),
    )
