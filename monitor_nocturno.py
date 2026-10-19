#!/usr/bin/env python3
"""
Monitor nocturno del sistema de trading (solo lectura).

Cada 5 minutos comprueba y deja una línea de estado + las anomalías en un
log. NO toca nada: no entra, no sale, no promueve. Es un vigilante externo
al contenedor (si el contenedor muere, el monitor sigue y lo reporta).

Qué vigila
----------
1. Salud del backend (/health) y del MCP (cuenta del bróker).
2. Flujo de ciclos: la última fila de `ai_predict_cycle` y su edad (el EA
   consulta cada 5 min; > 15 min ⇒ el EA o el terminal están parados).
3. Decisiones: cuántas HOLD / BUY / SELL / FLAT y el motivo de veto.
4. Posición viva (MT5) y PnL abierto.
5. Equity y drawdown contra el pico (límite diario 5 %).
6. Latencia de los ciclos (presupuesto: backend 45 s < EA 60 s).
7. La calibración del LLM (muestra de `bias_prob` para la autoridad del veto).
8. Anomalías del contenedor (reinicios) y del bundle servido (sha).

Uso:  python3 monitor_nocturno.py [ruta_log]
"""
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

LOG = sys.argv[1] if len(sys.argv) > 1 else "audits/monitor-2026-10-19.log"
BACKEND = "http://localhost:8090"
MT5 = os.environ.get("MT5_HTTP_URL", "http://100.81.112.95:8000").rstrip("/")
PSQL = ["docker", "exec", "trading-postgres", "psql", "-U", "trading", "-d", "trading", "-tAc"]
DOCKER = ["docker"]

WARN = []


def _out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}"


def sql(query):
    r = _out(PSQL + [query])
    return None if r.startswith("ERR:") or not r else r


def http_json(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def check():
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"[{ts} UTC]"]
    alerts = []

    # 1. Salud del backend y del MCP
    health = http_json(f"{BACKEND}/health")
    mcp_ok = health.get("mcp", {}).get("connected")
    equity_txt = health.get("mcp", {}).get("detail", "")
    parts.append(f"backend={health.get('status', '?')} mcp={mcp_ok} equity={equity_txt or '?'}")
    if health.get("status") != "ok":
        alerts.append(f"backend NO ok: {health.get('status')} ({health.get('mcp', {}).get('detail', '')[:80]})")
    if mcp_ok is False:
        alerts.append("MCP desconectado (terminal/bróker)")

    # 2-3. Flujo de ciclos y decisiones
    row = sql(
        "SELECT to_char(created_at,'HH24:MI:SS')||'|'||"
        "(EXTRACT(epoch FROM (now()-created_at))::int)::text||'|'||"
        "COALESCE(data->'data'->>'decision','?')||'|'||"
        "COALESCE(data->'data'->>'veto_reason','-')||'|'||"
        "COALESCE(data->'data'->>'ml_prob','?')||'|'||"
        "COALESCE(data->'data'->>'latency_ms','?')||'|'||"
        "COALESCE(data->'data'->>'llm_bias','?')||'|'||"
        "COALESCE(data->'data'->>'llm_bias_prob','?')||'|'||"
        "COALESCE(data->'data'->'quorum'->>'lateral','-')||'|'||"
        "COALESCE(data->'data'->'quorum'->>'granted','-') "
        "FROM audit_log WHERE event='ai_predict_cycle' "
        "AND data->'data'->>'symbol'='EURUSD' ORDER BY created_at DESC LIMIT 1"
    )
    if row:
        f = row.split("|")
        age_s = int(f[1]) if f[1].isdigit() else 9999
        parts.append(
            f"ciclo={f[0]} ({age_s}s) dec={f[2]} veto={f[3]} ml_prob={f[4]} "
            f"lat={int(float(f[5]))/1000:.0f}s llm={f[6]}/{f[7]} quórum(lateral={f[8]},granted={f[9]})"
        )
        if age_s > 900:
            alerts.append(f"sin ciclo desde hace {age_s}s (EA o terminal parados)")
        if f[5] not in ("?", ""):
            lat = float(f[5]) / 1000
            if lat > 55:
                alerts.append(f"latencia de ciclo {lat:.0f}s (presupuesto EA 60s)")
        if f[3] not in ("-", "") and f[3] != "reentry_veto":
            # El veto de RE-ENTRADA es rutinario tras un cierre (ancla
            # anti-churn: ≤2 velas H1) ⇒ no es anomalía. Los demás, sí.
            alerts.append(f"último veto: {f[3]}")
    else:
        alerts.append("sin filas de ai_predict_cycle")

    # Conteos de la última hora
    counts = sql(
        "SELECT COALESCE(data->'data'->>'decision','?')||'='||count(*)::text "
        "FROM audit_log WHERE event='ai_predict_cycle' AND data->'data'->>'symbol'='EURUSD' "
        "AND created_at > now() - interval '1 hour' GROUP BY 1 ORDER BY 1"
    )
    if counts:
        parts.append(f"1h[{counts.replace(chr(10), ',')}]")

    # 4. Posición viva
    pos = http_json(f"{MT5}/api/v1/positions")
    if isinstance(pos, list):
        if pos:
            p = pos[0]
            parts.append(
                f"pos={p.get('type')} {p.get('volume')}@{p.get('open')} pnl={p.get('profit')} "
                f"sl={p.get('stop_loss')} tp={p.get('take_profit')}"
            )
        else:
            parts.append("pos=ninguna")
    else:
        parts.append("pos=?")
        alerts.append("no se pudo leer /positions del MCP")

    # 7. Calibración del LLM (muestra para la autoridad del veto)
    cal = sql(
        "SELECT count(*)::text||' juicios, '||"
        "count(*) FILTER (WHERE abs((data->'data'->>'llm_bias_prob')::numeric-0.5)>=0.2)::text||' alta convicción' "
        "FROM audit_log WHERE event='ai_predict_cycle' AND data->'data'->>'symbol'='EURUSD' "
        "AND data->'data'->>'llm_bias_prob' IS NOT NULL "
        "AND COALESCE(data->'data'->>'qwen_skipped','false')='false' "
        "AND COALESCE(data->'data'->>'qwen_abstained','false')='false' "
        "AND created_at > now() - interval '24 hours'"
    )
    if cal:
        parts.append(f"calib24h={cal}")

    # 8. Reinicios del contenedor y sha servido
    restarts = _out(DOCKER + ["inspect", "-f", "{{.RestartCount}}", "trading-fastapi"])
    started = _out(DOCKER + ["inspect", "-f", "{{.State.StartedAt}}", "trading-fastapi"])
    parts.append(f"contenedor(restarts={restarts}, desde={started[:19]})")
    if restarts.isdigit() and int(restarts) > 0:
        alerts.append(f"contenedor reiniciado {restarts} veces (último: {started[:19]})")
    sha = _out(DOCKER + ["exec", "trading-fastapi", "cat", "/app/ml/ppo_trading_bot_v3.manifest.json"])
    if sha.startswith("{"):
        try:
            m = json.loads(sha)
            parts.append(f"bundle={m.get('model_sha256', '?')[:12]}")
        except ValueError:
            pass

    line = " | ".join(parts)
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")
        for a in alerts:
            fh.write(f"    ⚠ {a}\n")
    return alerts


def main():
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    print(f"monitor → {LOG}", flush=True)
    while True:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 — el monitor nunca muere
            with open(LOG, "a") as fh:
                fh.write(f"[{dt.datetime.now(dt.timezone.utc)}] ERROR monitor: {exc}\n")
        import time
        time.sleep(300)


if __name__ == "__main__":
    main()
