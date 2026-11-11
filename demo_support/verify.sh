#!/bin/sh
set -eu
npm run build
npm test
npm run acceptance
python demo_support/check.py
