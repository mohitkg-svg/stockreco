#!/usr/bin/env bash
set -e

if [ -f backend/.env ]; then
  export $(grep -v '^#' backend/.env | xargs)
fi

echo "🚀 Updating Live Auto-Trader Configuration..."
export PYTHONPATH=./backend:$PYTHONPATH
python3 << 'EOF'
import os
import sys
import subprocess
import time
import re

db_url = os.environ.get("DATABASE_URL", "")
proxy_proc = None

if "/cloudsql/" in db_url:
    m = re.search(r'/cloudsql/([^/?]+)', db_url)
    if m:
        instance_name = m.group(1)
        if "?host=/cloudsql/" in db_url:
            base = db_url.split("?")[0]
            new_url = base.replace("@/", "@localhost:5432/")
        else:
            new_url = db_url.replace(f"/cloudsql/{instance_name}", "localhost:5432")
            
        if not os.path.exists("cloud-sql-proxy"):
            subprocess.run(["curl", "-fLo", "cloud-sql-proxy", "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.8.0/cloud-sql-proxy.linux.amd64"], check=True)
            subprocess.run(["chmod", "+x", "cloud-sql-proxy"], check=True)
            
        proxy_proc = subprocess.Popen(["./cloud-sql-proxy", instance_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        os.environ["DATABASE_URL"] = new_url

sys.path.insert(0, os.path.abspath('backend'))
try:
    from database import SessionLocal, AutoTraderConfig
    db = SessionLocal()
    cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
    if cfg:
        cfg.confidence_threshold = 55.0
        cfg.ml_scoring_enabled = True
        cfg.twap_enabled = True
        db.commit()
        print("✅ Auto-Trader Config updated successfully:")
        print("   - Confidence Threshold: 55.0")
        print("   - ML Scoring: ENABLED")
        print("   - TWAP Execution: ENABLED")
    else:
        print("❌ Error: AutoTraderConfig row not found.")
    db.close()
finally:
    if proxy_proc:
        proxy_proc.terminate()
        proxy_proc.wait()
EOF