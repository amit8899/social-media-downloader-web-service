#!/usr/bin/env bash
set -e

echo "=== Node & Python Environment ==="
node --version || echo "Node not found"
npm --version || echo "npm not found"
python --version || python3 --version || echo "Python not found"

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo "=== Setting up bgutil PO provider ==="
rm -rf vendor/bgutil-ytdlp-pot-provider
mkdir -p vendor
git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git vendor/bgutil-ytdlp-pot-provider
cd vendor/bgutil-ytdlp-pot-provider/server
npm ci
npx tsc

echo "=== bgutil PO provider built successfully ==="
