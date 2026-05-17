#!/usr/bin/env bash
set -e

REGION="${1:-us-central1}"

echo "🚀 Deploying API Service (stockrecs)..."
chmod +x deploy.sh || true
bash deploy.sh "$REGION"

echo "🚀 Deploying Manager Service (stockrecs-manager)..."
chmod +x deploy-manager.sh || true
bash deploy-manager.sh "$REGION"

echo "✅ Both services deployed successfully!"