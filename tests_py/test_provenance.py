import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.provenance import PORTAL_COLUMNS, verify_download
from collector.errors import ProvenanceMismatch

FILTERS = {'org': 'b', 'period': '2026-10', 'metric': 'gross'}
EXPORT_ID = 'a' * 32


def portal_csv(overrides=None, *, header=None):
    row = {'export_id': EXPORT_ID, 'org': 'b', 'period': '2026-10',
           'metric': 'gross', 'record_id': 'r1', 'amount': '125.50'}
    row.update(overrides or {})
    columns = header or PORTAL_COLUMNS
    import csv
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerow({name: row.get(name, '') for name in columns})
    return ('﻿' + buffer.getvalue()).encode('utf-8')


class ProvenanceTest(unittest.TestCase):
    def test_matching_file_passes_and_is_hashed(self):
        result = verify_download(portal_csv(), EXPORT_ID, FILTERS)
        self.assertEqual(result['rowCount'], 1)
        self.assertRegex(result['sha256'], r'^[0-9a-f]{64}$')

    def test_late_download_from_previous_round_is_distinguished(self):
        # 列结构完全相同，但导出号属于上一轮——这正是运营事故的形态。
        late = portal_csv({'export_id': 'c' * 32, 'org': 'a',
                           'period': '2026-11', 'metric': 'net'})
        with self.assertRaises(ProvenanceMismatch) as caught:
            verify_download(late, EXPORT_ID, FILTERS)
        self.assertEqual(caught.exception.kind, 'provenance_mismatch')
        self.assertIn('不属于本轮', caught.exception.message)

    def test_filter_mismatch_and_bad_header_are_rejected(self):
        with self.assertRaises(ProvenanceMismatch):
            verify_download(portal_csv({'org': 'a'}), EXPORT_ID, FILTERS)
        with self.assertRaises(ProvenanceMismatch):
            verify_download(portal_csv(header=PORTAL_COLUMNS[::-1]), EXPORT_ID, FILTERS)
        with self.assertRaises(ProvenanceMismatch):
            verify_download(b'', EXPORT_ID, FILTERS)

    def test_filter_validation(self):
        with self.assertRaises(ProvenanceMismatch):
            verify_download(portal_csv(), EXPORT_ID, {**FILTERS, 'org': 'z'})
        with self.assertRaises(ProvenanceMismatch):
            verify_download(portal_csv(), 'too-short', FILTERS)


if __name__ == '__main__':
    unittest.main()
