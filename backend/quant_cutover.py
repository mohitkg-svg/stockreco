"""
One-shot script to prepare the database for the new Pure ML engine.
Flushes legacy TA signals and predictions, and updates the AutoTraderConfig.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import SessionLocal, AutoTraderConfig, Signal, MLPrediction

def cutover():
    db = SessionLocal()
    try:
        # 1. Update Config for ML Probabilities
        cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        if cfg:
            cfg.confidence_threshold = 55.0
            cfg.option_thesis_min_conf_aggressive = 55.0
            cfg.ml_scoring_enabled = True
            print("✅ Updated AutoTraderConfig for ML thresholds (55.0).")

        # 2. Flush legacy TA signals & predictions so they don't corrupt the calibrator
        del_preds = db.query(MLPrediction).delete()
        del_sigs = db.query(Signal).delete()
        print(f"✅ Flushed {del_preds} legacy ML predictions.")
        print(f"✅ Flushed {del_sigs} legacy TA signals.")

        db.commit()
        print("🚀 Quant engine cutover complete. You are ready to run /api/ml/train.")
    except Exception as e:
        db.rollback()
        print(f"❌ Error during cutover: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    cutover()