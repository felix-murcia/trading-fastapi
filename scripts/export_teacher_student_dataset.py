"""A/B data plumbing — Teacher–Student (T-17, fase A).

Reads ONLY `audit_log` (event='ai_predict_cycle') for the last N days and
emits one row per prediction cycle with both directional heads (PPO-style
signal from the backend + teacher output from Qwen) plus the veto/holdout
context. Output is a plain CSV to stdout.

READ-ONLY: never writes to DB, model or prompt.

Usage (inside the fastapi container):
    python /app/scripts/export_teacher_student_dataset.py --days 60 --out /tmp/ts.csv
"""
import argparse
import asyncio
import csv
import json
import os
import sys

import asyncpg

DSN = os.getenv("DATABASE_URL")

FIELDS = ("cycle_id", "exit_time", "symbol", "mode",
          "ppo_decision", "ppo_ml_prob",
          "qwen_bias", "qwen_quality", "qwen_regime",
          "veto_reason", "decision", "reward_below_min",
          "effective_prob", "eff_prob_raw", "conf_mod")


async def load(rows_days: int):
    conn = await asyncpg.connect(DSN)
    query = """
        SELECT created_at AT TIME ZONE 'UTC' AS dt, data
        FROM audit_log
        WHERE event = 'ai_predict_cycle'
          AND created_at > NOW() - (INTERVAL '1 day') * $1
        ORDER BY created_at ASC;
    """
    records = await conn.fetch(query, int(rows_days))
    await conn.close()
    return records


def flatten(r):
    raw = r["data"]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict):
        payload = {}
    core = payload.get("data", payload)
    exit_time = core.get("exit_time") or core.get("created_at")
    if exit_time and not isinstance(exit_time, str):
        try:
            exit_time = exit_time.isoformat()
        except Exception:
            exit_time = str(exit_time)
    mode = "on" if (core.get("memory") or {}).get("injected") else "shadow"
    if core.get("memory") is None:
        mode = "off"
    return {
        "cycle_id": core.get("cycle_id", ""),
        "exit_time": exit_time or r["dt"].isoformat(),
        "symbol": core.get("symbol", "EURUSD"),
        "mode": mode,
        "ppo_decision": core.get("decision") if False else core.get("ppo_decision") or core.get("decision"),
        "ppo_ml_prob": core.get("ml_prob") or core.get("ml_signal"),
        "qwen_bias": core.get("llm_bias") or core.get("qwen_bias"),
        "qwen_quality": core.get("quality_score") or core.get("quality"),
        "qwen_regime": core.get("regime") or core.get("qwen_regime"),
        "veto_reason": core.get("veto_reason"),
        "decision": core.get("decision"),
        "reward_below_min": bool(core.get("reward_below_min")),
        "effective_prob": core.get("effective_prob"),
        "eff_prob_raw": core.get("eff_prob_raw"),
        "conf_mod": core.get("confidence_modifier"),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    rows = await load(args.days)
    out = sys.stdout if not args.out else open(args.out, "w", newline="")
    w = csv.DictWriter(out, fieldnames=FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow(flatten(r))
    if args.out:
        out.close()
    print(f"# rows={len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
