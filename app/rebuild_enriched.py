"""
One-off: force-rebuild the enriched MLB historical dataset with the new
30-feature vector (adds BB/9, home/away ERA splits, last-3-start ERA,
pitcher dominance composite, lineup vuln slot, blowout probability).
Run from the app/ directory so .cache resolves correctly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.enriched_historical_data import build_enriched_dataset

print("Forcing rebuild of enriched MLB dataset (30 features)...")
X, y_ml, y_rl, totals = build_enriched_dataset(force_rebuild=True)
print(f"\nDone. X.shape={X.shape}  y_ml.shape={y_ml.shape}  y_rl.shape={y_rl.shape}")
print(f"feature count: {X.shape[1]}")
