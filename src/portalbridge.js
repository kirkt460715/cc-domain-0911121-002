import {HarborError, demand} from './primitives.js';
import {csvTable} from './tabular.js';

// 合作方演练门户 CSV 的固定来源契约。门户导出的列由 portal/server.py 冻结，
// 采集侧只接受同一组列，并通过显式映射进入 DataHarbor 数据集。
export const PORTAL_OWNER = 'local';
export const PORTAL_STEWARD = 'alice';
export const PORTAL_DATASET = 'partner_metrics';
export const PORTAL_COLUMNS = ['export_id', 'org', 'period', 'metric', 'record_id', 'amount'];
export const PORTAL_MAPPING = PORTAL_COLUMNS.map(source => ({target: source, source}));
export const PORTAL_GATE = {minimumRows: 1, maximumRejectRate: 0};
const EXPORT_ID_PATTERN = /^[a-f0-9]{32}$/;
const PERIOD_PATTERN = /^[0-9]{4}-[0-2][0-9]$/;

export function portalSchema() {
  return {
    columns: [
      {name: 'export_id', type: 'string', classification: 'public', maxLength: 64},
      {name: 'org', type: 'enum', classification: 'public', values: ['a', 'b']},
      {name: 'period', type: 'string', classification: 'public', maxLength: 16},
      {name: 'metric', type: 'enum', classification: 'public', values: ['net', 'gross']},
      {name: 'record_id', type: 'string', classification: 'public', maxLength: 128},
      {name: 'amount', type: 'number', classification: 'public', minimum: 0},
    ],
    primaryKey: ['export_id'],
  };
}

export function provenanceContract() {
  return {
    owner: PORTAL_OWNER,
    steward: PORTAL_STEWARD,
    dataset: PORTAL_DATASET,
    columns: [...PORTAL_COLUMNS],
    mapping: PORTAL_MAPPING,
    gate: {...PORTAL_GATE},
    schema: portalSchema(),
  };
}

// 同一组筛选的反复采集必须命中同一 importId，使 Harbor 内置的 replay 机制
// 天然保证“重试不重复导入”。
export function importIdForFilter(filters) {
  demand(filters && typeof filters === 'object', 'portal_filter', 'Portal filters are required');
  for (const name of ['org', 'period', 'metric']) {
    demand(typeof filters[name] === 'string' && filters[name].length > 0, 'portal_filter',
      `Portal filter ${name} is missing`);
  }
  demand(['a', 'b'].includes(filters.org), 'portal_filter', 'Unsupported portal org', {org: filters.org});
  demand(PERIOD_PATTERN.test(filters.period), 'portal_filter', 'Portal period must look like YYYY-MM',
    {period: filters.period});
  demand(['net', 'gross'].includes(filters.metric), 'portal_filter', 'Unsupported portal metric',
    {metric: filters.metric});
  return `portal-${filters.org}-${filters.period}-${filters.metric}`;
}

// 幂等准备：机构与数据集只注册一次，重复执行返回已有状态。
export async function bootstrapPortal(harbor) {
  const catalog = await harbor.catalog();
  const created = [];
  if (!catalog.organizations.some(item => item.id === PORTAL_OWNER)) {
    await harbor.organization({id: PORTAL_OWNER, name: 'Synthetic local institute', region: 'south',
      purposes: ['research'], stewards: [PORTAL_STEWARD]}, 'bootstrap');
    created.push('organization');
  }
  const latest = await harbor.catalog();
  if (!latest.datasets.some(item => item.id === PORTAL_DATASET)) {
    await harbor.dataset({id: PORTAL_DATASET, name: 'Partner portal metric exports',
      owner: PORTAL_OWNER, schema: portalSchema(), gate: PORTAL_GATE}, PORTAL_STEWARD);
    created.push('dataset');
  }
  return {created, dataset: PORTAL_DATASET, owner: PORTAL_OWNER, steward: PORTAL_STEWARD};
}

// 在创建任何数据版本之前，先核对文件自身携带的导出标识与冻结筛选信息。
// 浏览器层已经核对一次，这里是进入 Harbor 前的第二道来源校验。
export function verifyPortalCsv(csv, exportId, filters) {
  demand(typeof csv === 'string' && csv.trim().length > 0, 'portal_csv', 'Portal CSV content is empty');
  demand(EXPORT_ID_PATTERN.test(exportId), 'portal_export', 'Portal export id is malformed', {exportId});
  const expectedImportId = importIdForFilter(filters);
  let rows;
  try {
    rows = csvTable(csv, ',');
  } catch (error) {
    if (error instanceof HarborError) {
      throw new HarborError('portal_csv', `Portal CSV cannot be parsed: ${error.message}`, error.context);
    }
    throw error;
  }
  demand(rows.length > 0, 'portal_csv', 'Portal CSV contains no data rows');
  const header = Object.keys(rows[0]);
  demand(header.length === PORTAL_COLUMNS.length &&
    PORTAL_COLUMNS.every((name, index) => name === header[index]), 'portal_header',
    'Portal CSV columns do not match the provenance contract', {header, expected: PORTAL_COLUMNS});
  rows.forEach((row, index) => {
    demand(row.export_id === exportId, 'portal_provenance',
      'CSV export_id does not belong to this round', {index, exportId, actual: row.export_id});
    for (const name of ['org', 'period', 'metric']) {
      demand(row[name] === filters[name], 'portal_provenance',
        `CSV ${name} does not match the confirmed page selection`,
        {index, expected: filters[name], actual: row[name]});
    }
  });
  return {rows, importId: expectedImportId};
}

export async function receivePortalExport(harbor, input, actor = PORTAL_STEWARD) {
  demand(input && typeof input === 'object', 'shape', 'Receive input must be an object');
  const {csv, filters, exportId, runId} = input;
  const verified = verifyPortalCsv(csv, exportId, filters);
  const result = await harbor.import({
    dataset: PORTAL_DATASET,
    importId: verified.importId,
    content: csv,
    format: 'csv',
    mapping: PORTAL_MAPPING,
    coerce: true,
  }, actor);
  const version = result.version;
  return {
    runId: runId ?? null,
    exportId,
    importId: verified.importId,
    reference: version.reference,
    status: version.status,
    replay: result.replay === true,
    committed: result.committed === true,
    rows: version.rows,
    gate: version.gate,
    report: version.report,
  };
}

// 从门户导出号或采集 importId 反查 DataHarbor 版本，保证双向可追溯。
export async function tracePortalExport(vault, index, query) {
  demand(query && typeof query === 'object', 'shape', 'Trace query must be an object');
  const {exportId, importId} = query;
  demand(Boolean(exportId) !== Boolean(importId), 'portal_trace',
    'Provide exactly one of exportId or importId');
  if (exportId) {
    demand(EXPORT_ID_PATTERN.test(exportId), 'portal_export', 'Portal export id is malformed', {exportId});
  }
  const candidates = Object.values(index.versions).filter(version => version.dataset === PORTAL_DATASET);
  for (const version of candidates) {
    if (importId && version.importId !== importId) {
      continue;
    }
    const raw = await vault.getRows(version.rawObject);
    if (exportId && !raw.some(row => row.export_id === exportId)) {
      continue;
    }
    return {
      found: true,
      exportId: exportId ?? raw[0]?.export_id ?? null,
      importId: version.importId,
      reference: version.reference,
      status: version.status,
      rows: version.rows,
      createdBy: version.createdBy,
      createdAt: version.createdAt,
      inputHash: version.inputHash,
    };
  }
  return {found: false, exportId: exportId ?? null, importId: importId ?? null};
}
