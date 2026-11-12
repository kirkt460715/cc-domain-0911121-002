"""门户 CSV 的来源校验（Python 侧，浏览器下载之后、进入 Harbor 之前）。

文件被视为不可信数据：只用标准库解析，不执行其中任何内容。
"""

import csv
import hashlib
import io
import re

from .errors import ProvenanceMismatch

PORTAL_COLUMNS = ['export_id', 'org', 'period', 'metric', 'record_id', 'amount']
EXPORT_ID_RE = re.compile(r'^[a-f0-9]{32}$')
PERIOD_RE = re.compile(r'^[0-9]{4}-[0-2][0-9]$')
ORGS = ('a', 'b')
METRICS = ('net', 'gross')


def validate_filters(filters):
    if not isinstance(filters, dict) or set(filters) != {'org', 'period', 'metric'}:
        raise ProvenanceMismatch('筛选条件必须且只能包含 org、period、metric', {'filters': filters})
    if filters['org'] not in ORGS:
        raise ProvenanceMismatch('机构取值不在门户约定范围内', {'org': filters['org']})
    if not PERIOD_RE.match(filters['period']):
        raise ProvenanceMismatch('期间必须形如 YYYY-MM', {'period': filters['period']})
    if filters['metric'] not in METRICS:
        raise ProvenanceMismatch('口径取值不在门户约定范围内', {'metric': filters['metric']})


def read_portal_csv(raw_bytes):
    """按门户固定列读取下载字节，返回 (行列表, sha256)。"""
    if not raw_bytes:
        raise ProvenanceMismatch('下载文件为空', {})
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        text = raw_bytes.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ProvenanceMismatch('CSV 不是 UTF-8 文本', {'cause': str(exc)}) from exc
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ProvenanceMismatch('CSV 缺少表头', {})
    if header != PORTAL_COLUMNS:
        raise ProvenanceMismatch('CSV 列结构与门户来源契约不一致',
                                 {'header': header, 'expected': PORTAL_COLUMNS})
    rows = []
    for line, values in enumerate(reader, start=2):
        if len(values) != len(PORTAL_COLUMNS):
            raise ProvenanceMismatch('CSV 行宽与表头不一致', {'line': line, 'width': len(values)})
        rows.append(dict(zip(PORTAL_COLUMNS, values)))
    if not rows:
        raise ProvenanceMismatch('CSV 没有数据行', {})
    return rows, digest


def verify_download(raw_bytes, expected_export_id, expected_filters):
    """核对文件自身的导出标识与冻结筛选信息，属于本轮才放行。"""
    validate_filters(expected_filters)
    if not EXPORT_ID_RE.match(expected_export_id):
        raise ProvenanceMismatch('本轮导出号格式非法', {'exportId': expected_export_id})
    rows, digest = read_portal_csv(raw_bytes)
    for index, row in enumerate(rows):
        if row['export_id'] != expected_export_id:
            # 上一轮迟到的下载会在这里被区分出来，即使列结构完全相同。
            raise ProvenanceMismatch('CSV 的导出号不属于本轮，疑似上一轮迟到文件',
                                     {'index': index, 'expectedExportId': expected_export_id,
                                      'actualExportId': row['export_id']})
        for name in ('org', 'period', 'metric'):
            if row[name] != expected_filters[name]:
                raise ProvenanceMismatch(
                    f'CSV 的 {name} 与页面实际确认的筛选不一致',
                    {'index': index, 'filter': name,
                     'expected': expected_filters[name], 'actual': row[name]})
    return {'rows': rows, 'sha256': digest, 'rowCount': len(rows)}
