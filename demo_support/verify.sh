#!/bin/sh
set -eu
npm run build
npm test
npm run acceptance
python demo_support/check.py
python -m py_compile collector/*.py tests_py/*.py
python -I tests_py/test_provenance.py
python -I tests_py/test_browser.py
python -I collector/drill.py
