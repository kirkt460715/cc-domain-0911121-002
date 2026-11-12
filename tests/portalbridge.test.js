import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Harbor} from '../src/service.js';
import {
  PORTAL_COLUMNS, PORTAL_MAPPING, portalSchema, provenanceContract, importIdForFilter,
  bootstrapPortal, verifyPortalCsv, receivePortalExport, tracePortalExport,
} from '../src/portalbridge.js';
import {kind} from './support.js';

const FILTERS = {org: 'b', period: '2026-10', metric: 'gross'};
const EXPORT_ID = 'a'.repeat(32);

function csv(overrides = {}) {
  const row = {export_id: EXPORT_ID, org: 'b', period: '2026-10', metric: 'gross',
    record_id: 'r1', amount: '125.50', ...overrides};
  return '﻿' + [PORTAL_COLUMNS.join(','), PORTAL_COLUMNS.map(name => row[name]).join(',')].join('\n') + '\n';
}

async function bootstrapped() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'harbor-portal-'));
  const harbor = new Harbor(root);
  await bootstrapPortal(harbor);
  return {root, harbor};
}

test('bootstrap is idempotent and freezes the portal provenance contract', async t => {
  const {root, harbor} = await bootstrapped();
  t.after(() => rm(root, {recursive: true, force: true}));
  const first = await bootstrapPortal(harbor);
  const second = await bootstrapPortal(harbor);
  assert.deepEqual(first.created, []);
  assert.deepEqual(second.created, []);
  const catalog = await harbor.catalog();
  const dataset = catalog.datasets.find(item => item.id === 'partner_metrics');
  assert.equal(dataset.owner, 'local');
  assert.equal(dataset.versions, 0);
  const contract = provenanceContract();
  assert.deepEqual(contract.columns, PORTAL_COLUMNS);
  assert.deepEqual(contract.mapping, PORTAL_MAPPING);
  assert.deepEqual(contract.schema, portalSchema());
});

test('import id is deterministic for the same confirmed filters', () => {
  const one = importIdForFilter(FILTERS);
  const two = importIdForFilter({...FILTERS});
  assert.equal(one, 'portal-b-2026-10-gross');
  assert.equal(one, two);
  assert.notEqual(one, importIdForFilter({...FILTERS, org: 'a'}));
  assert.throws(() => importIdForFilter({org: 'z', period: '2026-10', metric: 'net'}), kind('portal_filter'));
  assert.throws(() => importIdForFilter({org: 'a', period: '202610', metric: 'net'}), kind('portal_filter'));
});

test('verification rejects files that do not carry this round identity', () => {
  const parsed = verifyPortalCsv(csv(), EXPORT_ID, FILTERS);
  assert.equal(parsed.rows.length, 1);
  assert.equal(parsed.importId, 'portal-b-2026-10-gross');
  // 列结构相同也不能通过：来源标识必须属于本轮。
  assert.throws(() => verifyPortalCsv(csv(), 'b'.repeat(32), FILTERS), kind('portal_provenance'));
  // 页面确认的机构与文件冻结的机构不一致。
  assert.throws(() => verifyPortalCsv(csv(), EXPORT_ID, {...FILTERS, org: 'a'}), kind('portal_provenance'));
  // 迟到的旧导出：export_id 与筛选同时对不上。
  assert.throws(() => verifyPortalCsv(
    csv({export_id: 'c'.repeat(32), org: 'a', period: '2026-11', metric: 'net'}),
    EXPORT_ID, FILTERS), kind('portal_provenance'));
  const mutated = csv().replace('125.50', '999.00');
  const tampered = verifyPortalCsv(mutated, EXPORT_ID, FILTERS);
  assert.equal(tampered.rows[0].amount, '999.00');
});

test('receive creates a version once and replays retries without a second import', async t => {
  const {root, harbor} = await bootstrapped();
  t.after(() => rm(root, {recursive: true, force: true}));
  const first = await receivePortalExport(harbor, {csv: csv(), exportId: EXPORT_ID, filters: FILTERS,
    runId: 'run-1'});
  assert.equal(first.reference, 'partner_metrics:1');
  assert.equal(first.status, 'validated');
  assert.equal(first.replay, false);
  assert.equal(first.committed, true);
  assert.equal(first.rows, 1);
  assert.equal(first.exportId, EXPORT_ID);
  // 重试（同一筛选，门户可能给出新的 export_id，但 importId 相同）。
  const retry = await receivePortalExport(harbor, {csv: csv(), exportId: EXPORT_ID, filters: FILTERS,
    runId: 'run-2'});
  assert.equal(retry.replay, true);
  assert.equal(retry.committed, false);
  assert.equal(retry.reference, first.reference);
  const catalog = await harbor.catalog();
  assert.equal(catalog.datasets[0].versions, 1);
});

test('steward permission is enforced before any version is written', async t => {
  const {root, harbor} = await bootstrapped();
  t.after(() => rm(root, {recursive: true, force: true}));
  await assert.rejects(
    receivePortalExport(harbor, {csv: csv(), exportId: EXPORT_ID, filters: FILTERS}, 'carol'),
    kind('steward'));
  const catalog = await harbor.catalog();
  assert.equal(catalog.datasets[0].versions, 0);
});

test('failing quality gate keeps the file evidence in a quarantined version', async t => {
  const {root, harbor} = await bootstrapped();
  t.after(() => rm(root, {recursive: true, force: true}));
  const badAmount = csv({amount: '-7.00'});
  const received = await receivePortalExport(harbor, {csv: badAmount, exportId: EXPORT_ID, filters: FILTERS});
  assert.equal(received.status, 'quarantined');
  assert.equal(received.gate.passed, false);
  assert.ok(received.gate.failures.some(failure => failure.code === 'rejection_rate'));
  const rejected = await harbor.rejected(received.reference, 'alice');
  assert.equal(rejected.rejected[0].code, 'number');
  // 同一 importId 重放仍然指向隔离版本，不会产生第二条记录。
  const replay = await receivePortalExport(harbor, {csv: badAmount, exportId: EXPORT_ID, filters: FILTERS});
  assert.equal(replay.replay, true);
  assert.equal(replay.status, 'quarantined');
});

test('trace resolves both directions between portal export and harbor version', async t => {
  const {root, harbor} = await bootstrapped();
  t.after(() => rm(root, {recursive: true, force: true}));
  await receivePortalExport(harbor, {csv: csv(), exportId: EXPORT_ID, filters: FILTERS});
  const byExport = await harbor.read(index => tracePortalExport(harbor.vault, index, {exportId: EXPORT_ID}));
  assert.equal(byExport.found, true);
  assert.equal(byExport.reference, 'partner_metrics:1');
  assert.equal(byExport.importId, 'portal-b-2026-10-gross');
  const byImport = await harbor.read(index =>
    tracePortalExport(harbor.vault, index, {importId: 'portal-b-2026-10-gross'}));
  assert.equal(byImport.exportId, EXPORT_ID);
  const missing = await harbor.read(index =>
    tracePortalExport(harbor.vault, index, {exportId: 'f'.repeat(32)}));
  assert.equal(missing.found, false);
});

test('cli receive and trace work with real temp files outside the vault', async t => {
  const {root, harbor} = await bootstrapped();
  t.after(() => rm(root, {recursive: true, force: true}));
  const downloadDir = await mkdtemp(path.join(os.tmpdir(), 'portal-download-'));
  t.after(() => rm(downloadDir, {recursive: true, force: true}));
  const inputDir = await mkdtemp(path.join(os.tmpdir(), 'portal-input-'));
  t.after(() => rm(inputDir, {recursive: true, force: true}));
  const csvPath = path.join(downloadDir, `${EXPORT_ID}.csv`);
  const inputPath = path.join(inputDir, 'receive.json');
  await writeFile(csvPath, csv(), 'utf8');
  await writeFile(inputPath, JSON.stringify({exportId: EXPORT_ID, filters: FILTERS, runId: 'cli-1'}));
  const {runPortalCli} = await import('../src/portal-cli.js');
  const received = await runPortalCli(['portal-receive', '--root', root, '--actor', 'alice',
    '--input', inputPath, '--csv', csvPath]);
  assert.equal(received.reference, 'partner_metrics:1');
  const traceInput = path.join(inputDir, 'trace.json');
  await writeFile(traceInput, JSON.stringify({exportId: EXPORT_ID}));
  const traced = await runPortalCli(['portal-trace', '--root', root, '--input', traceInput]);
  assert.equal(traced.found, true);
  assert.equal(traced.reference, 'partner_metrics:1');
});
