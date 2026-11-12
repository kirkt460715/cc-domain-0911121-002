"""基于真实浏览器的合作方门户采集器。

设计约束（对应运营事故与验收要求）：
* 不按元素位置点击、不固定 sleep：所有等待都针对 DOM/网络的实际状态。
* 机构切换会异步刷新期间/口径候选项，页面用 generation 防止旧响应覆盖；
  采集器同样等待“当前选中项”真正反映本次选择后才继续。
* 下载列表按 data-export-id 精确匹配本轮新条目，上一轮的迟到条目不会被误取。
* 每次运行创建独立浏览器上下文，同一机构的两次运行不共享会话选择状态。
* 登录失效、筛选项撤回都转成带阶段与解释的结构化错误，停在可理解的位置。
"""

import json
import time

from .errors import (
    CollectorError,
    ExportTimeout,
    FilterWithdrawn,
    LoginExpired,
    OptionsStale,
    PortalUnavailable,
)

STATE_EXPIRED = '登录已失效'
STATE_WAIT_LOGIN = '请先进入账号'
DEFAULT_TIMEOUT_MS = 20000
POLL_MS = 50


class Faults:
    """演练故障注入。生产运行保持全部关闭。

    withdraw_filter：让 /select 返回 422，模拟筛选项被服务端撤回；
    expire_session：点击进入后结束会话，模拟登录失效；
    export_delay：让 /export 携带延迟，模拟后台生成晚到；
    fulfill_download：用自定义 CSV 字节替代真实下载，模拟下载被调包；
    interrupt_before_download：生成导出后不下载直接中断，模拟流程被打断。
    """

    def __init__(self, *, withdraw_filter=False, expire_session=False, export_delay=None,
                 fulfill_download=None, interrupt_before_download=False):
        self.withdraw_filter = withdraw_filter
        self.expire_session = expire_session
        self.export_delay = export_delay
        self.fulfill_download = fulfill_download
        self.interrupt_before_download = interrupt_before_download


class PortalSession:
    """一个隔离的浏览器会话：独立 cookie，与其他会话互不可见。"""

    def __init__(self, playwright, base_url, *, headed=False, faults=None, timeout_ms=DEFAULT_TIMEOUT_MS):
        self.base_url = base_url.rstrip('/')
        self.faults = faults or Faults()
        self.timeout_ms = timeout_ms
        self.browser = playwright.chromium.launch(headless=not headed, args=['--no-sandbox'])
        self.context = self.browser.new_context(accept_downloads=True)
        self.page = self.context.new_page()
        self.session_id = None
        # 筛选项撤回故障只在登录完成后生效，否则会误伤首屏自身的 /select。
        self.armed = False
        self._install_routes()

    def close(self):
        self.context.close()
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _install_routes(self):
        faults = self.faults

        def handle_route(route):
            request = route.request
            if request.post_data and request.url.endswith('/select') and \
                    self.armed and faults.withdraw_filter:
                route.fulfill(status=422,
                              content_type='application/json',
                              body=json.dumps({'error': '筛选项已撤回'}))
                return
            if request.post_data and request.url.endswith('/export'):
                if faults.export_delay is not None:
                    payload = json.loads(request.post_data or '{}')
                    payload['delay'] = faults.export_delay
                    route.continue_(post_data=json.dumps(payload),
                                    headers={**request.headers,
                                             'Content-Type': 'application/json'})
                    return
                if faults.fulfill_download is not None and \
                        faults.fulfill_download.get('replace', True):
                    # 替换模式：连导出条目也用故障导出号。
                    export_id = faults.fulfill_download['exportId']
                    route.fulfill(status=202, content_type='application/json',
                                  body=json.dumps({'export_id': export_id,
                                                   'org': faults.fulfill_download['filters']['org'],
                                                   'period': faults.fulfill_download['filters']['period'],
                                                   'metric': faults.fulfill_download['filters']['metric']}))
                    return
                # 调包模式：POST 放行，页面拿到服务端真实导出号，仅替换下载内容。
            if faults.fulfill_download is not None and '/export/' in request.url and request.method == 'GET':
                item = faults.fulfill_download
                if item.get('replace', True):
                    filename = f"{item['exportId']}.csv"
                else:
                    # 调包模式：保留服务端真实导出号作为文件名，只替换文件内容，
                    # 来源校验必须依靠 CSV 内部的 export_id 才能识破。
                    filename = request.url.rstrip('/').rsplit('/', 1)[-1] + '.csv'
                route.fulfill(status=200, content_type='text/csv',
                              headers={'Content-Disposition': f'attachment; filename={filename}'},
                              body=item['csv'].encode('utf-8-sig'))
                return
            route.continue_()

        self.page.route('**/*', handle_route)

    # -- 基础等待工具 -----------------------------------------------------

    def _wait(self, predicate, message, context):
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                value = predicate()
            except Exception:
                value = None
            if value:
                return value
            self.page.wait_for_timeout(POLL_MS)
        raise ExportTimeout(message, context)

    def _status_text(self):
        return self.page.locator('#status').inner_text()

    def _raise_if_expired(self, action):
        if self._status_text() == STATE_EXPIRED:
            raise LoginExpired('登录已失效，采集停在可重新登录的位置', {'action': action})

    def _cookies(self):
        return [cookie for cookie in self.context.cookies() if cookie['name'] == 'portal']

    # -- 页面步骤 ---------------------------------------------------------

    def open(self):
        try:
            self.page.goto(self.base_url, wait_until='domcontentloaded')
        except Exception as exc:
            raise PortalUnavailable(f'无法打开门户 {self.base_url}', {'cause': str(exc)}) from exc
        return self

    def login(self):
        self.page.get_by_role('button', name='进入演练账号').click()
        # 候选项加载完成后导出按钮才会启用；以它作为“账号已进入且首屏同步完成”的信号。
        self._wait(lambda: self.page.locator('#export').is_enabled(),
                   '登录后首屏候选项未就绪', {'url': self.base_url})
        cookies = self._cookies()
        if not cookies:
            raise LoginExpired('登录后未建立会话 cookie', {})
        self.session_id = cookies[0]['value']
        self.armed = True
        if self.faults.expire_session:
            # 通过页面自身按钮结束会话，模拟真实的失效操作。
            self.page.get_by_role('button', name='结束会话').click()
            self._wait(lambda: self._status_text() == STATE_EXPIRED,
                       '结束会话后状态未更新', {})
            raise LoginExpired('演练故障：会话已被结束', {'action': 'login'})
        return self.session_id

    def _selected(self):
        node = self.page.locator('#selected')
        if node.inner_text() == '尚未选择':
            return None
        return {'org': node.get_attribute('data-org'),
                'text': node.inner_text()}

    def _wait_selected(self, org, period, metric, action):
        expected = f'{org} / {period} / {metric}'

        def match():
            selected = self._selected()
            return selected if selected and selected['text'] == expected else None

        try:
            return self._wait(match, f'页面确认的筛选未达到 {expected}', {'action': action})
        except ExportTimeout as exc:
            self._raise_if_expired(action)
            raise OptionsStale(exc.message, exc.context) from exc

    def _option_values(self, selector):
        return self.page.eval_on_selector_all(
            selector + ' option', 'nodes => nodes.map(node => node.value)')

    def select_org(self, org):
        try:
            self.page.select_option('#org', org)
            # 机构变化会先禁用导出按钮、异步拉取新候选项再重新 sync；
            # 等按钮重新启用且 #selected 反映新机构，杜绝旧候选响应覆盖。
            self._wait(lambda: self.page.locator('#export').is_enabled(),
                       '机构切换后候选项未刷新完成', {'org': org})
        except ExportTimeout as exc:
            self._raise_if_expired('select_org')
            status = self._status_text()
            if status and status != STATE_WAIT_LOGIN:
                raise FilterWithdrawn(f'服务端拒绝了筛选：{status}', {'status': status,
                                                                     'org': org}) from exc
            raise
        selected = self._wait_selected(org,
                                       self.page.locator('#period').input_value(),
                                       self.page.locator('#metric').input_value(),
                                       'select_org')
        return {'org': org, 'periodOptions': self._option_values('#period'),
                'metricOptions': self._option_values('#metric'),
                'confirmed': selected['text']}

    def select_period(self, period):
        available = self._option_values('#period')
        if period not in available:
            raise FilterWithdrawn('期间候选项已被撤回，页面上没有该选项',
                                  {'period': period, 'available': available})
        self.page.select_option('#period', period)
        return self._after_filter_change(period, '#period', 'period')

    def select_metric(self, metric):
        available = self._option_values('#metric')
        if metric not in available:
            raise FilterWithdrawn('口径候选项已被撤回，页面上没有该选项',
                                  {'metric': metric, 'available': available})
        self.page.select_option('#metric', metric)
        return self._after_filter_change(metric, '#metric', 'metric')

    def _after_filter_change(self, value, selector, label):
        try:
            self._wait(lambda: self.page.locator('#export').is_enabled(),
                       f'{label} 变更后页面未重新同步', {'value': value})
        except ExportTimeout as exc:
            self._raise_if_expired(label)
            raise
        self._raise_if_expired(label)
        org = self.page.locator('#org').input_value()
        period = self.page.locator('#period').input_value()
        metric = self.page.locator('#metric').input_value()
        confirmed = self._wait_selected(org, period, metric, label)
        return {'org': org, 'period': period, 'metric': metric, 'confirmed': confirmed['text']}

    def confirmed_filters(self):
        """触发任何导出前，重新读取页面实际选中的三项。"""
        org = self.page.locator('#org').input_value()
        period = self.page.locator('#period').input_value()
        metric = self.page.locator('#metric').input_value()
        confirmed = self._wait_selected(org, period, metric, 'confirmed_filters')
        return {'org': org, 'period': period, 'metric': metric, 'confirmed': confirmed['text']}

    def _existing_export_ids(self):
        return set(self.page.eval_on_selector_all(
            '#downloads li[data-export-id]',
            'nodes => nodes.map(node => node.dataset.exportId)'))

    def trigger_export(self):
        before = self._existing_export_ids()
        self.page.locator('#export').click()

        def new_entry():
            current = self._existing_export_ids()
            fresh = current - before
            return next(iter(fresh)) if fresh else None

        try:
            export_id = self._wait(new_entry, '导出条目未出现在下载列表', {'before': sorted(before)})
        except ExportTimeout as exc:
            self._raise_if_expired('trigger_export')
            # /select 在表单提交时同步调用，422 会把错误写到状态栏。
            status = self._status_text()
            if status and status != STATE_WAIT_LOGIN:
                raise FilterWithdrawn(f'服务端拒绝了筛选：{status}', {'status': status}) from exc
            raise
        self._raise_if_expired('trigger_export')
        return export_id

    def wait_for_download_link(self, export_id):
        # 只等本轮条目自己的锚点，列表中更早的迟到条目不参与匹配。
        selector = f'#downloads li[data-export-id="{export_id}"] a'
        try:
            self.page.locator(selector).wait_for(state='visible', timeout=self.timeout_ms)
        except Exception as exc:
            self._raise_if_expired('wait_for_download_link')
            raise ExportTimeout('后台导出未在时限内生成下载', {'exportId': export_id}) from exc
        return self.page.locator(selector)

    def download(self, export_id):
        if self.faults.interrupt_before_download:
            raise CollectorError('演练故障：导出已生成，下载前中断',
                                 {'exportId': export_id, 'interrupted': True})
        link = self.wait_for_download_link(export_id)
        with self.page.expect_download(timeout=self.timeout_ms) as event_info:
            link.click()
        download = event_info.value
        suggested = download.suggested_filename
        if suggested != f'{export_id}.csv':
            raise FilterWithdrawn('下载文件名与本轮导出号不一致',
                                  {'exportId': export_id, 'suggested': suggested})
        return download.path(), suggested

    def fetch_export_status(self, export_id):
        """供演练核对：另一个会话读取旧导出号必须被服务端拒绝。"""
        response = self.context.request.get(f'{self.base_url}/export/{export_id}')
        return response.status


def collect(playwright, base_url, filters, *, headed=False, faults=None, timeout_ms=DEFAULT_TIMEOUT_MS):
    """执行一轮完整页面采集，返回带浏览器证据的结果字典。"""
    faults = faults or Faults()
    with PortalSession(playwright, base_url, headed=headed, faults=faults,
                       timeout_ms=timeout_ms) as session:
        session.open()
        session.login()
        session.select_org(filters['org'])
        session.select_period(filters['period'])
        session.select_metric(filters['metric'])
        # 导出前最后确认一次页面实际选中值。
        confirmed = session.confirmed_filters()
        export_id = session.trigger_export()
        local_path, filename = session.download(export_id)
        with open(local_path, 'rb') as handle:
            raw = handle.read()
        return {'sessionId': session.session_id, 'exportId': export_id,
                'filename': filename, 'rawBytes': raw,
                'confirmed': {'org': confirmed['org'], 'period': confirmed['period'],
                              'metric': confirmed['metric'], 'text': confirmed['confirmed']}}
