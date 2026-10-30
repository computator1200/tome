"""Re-translate a fixed sample of a volume and compare it with what is stored.

Run before changing the model or the prompts: it uses the current settings,
writes nothing, and exits non-zero when the mean similarity falls below the
threshold, so it can gate a change.

Run from backend/:
    python scripts/regression_sample.py <volume_id> [--pages 5] [--threshold 0.6]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import SessionLocal, init_db  # noqa: E402
from services import provider, quality  # noqa: E402


async def main(volume_id: str, pages: int, threshold: float) -> int:
    await init_db()
    await provider.load_from_db()
    async with SessionLocal() as session:
        report = await quality.regression_sample(session, volume_id, pages)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    mean = report["mean_similarity"]
    if mean is None:
        print("no page could be re-translated", file=sys.stderr)
        return 2
    if mean < threshold:
        print(f"mean similarity {mean:.2f} is below {threshold:.2f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("volume_id")
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.6)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.volume_id, args.pages, args.threshold)))
