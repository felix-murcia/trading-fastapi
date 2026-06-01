"""
Genera y deploya los 7 workflows n8n para el sistema de trading Forex v4.0.

Uso:
    python3 generate.py             # solo genera los JSON
    python3 generate.py --deploy    # genera + deploya en n8n vía API
"""
import json
import uuid
import os
import sys
import urllib.request
import urllib.error

BASE     = os.path.dirname(os.path.abspath(__file__))
FASTAPI  = "http://127.0.0.1:8090"
IDS_FILE = os.path.join(BASE, "workflow-ids.json")
ENV_FILE = os.path.join(os.path.dirname(BASE), ".env")


# ── lectura de .env ────────────────────────────────────────────────────────────

def _read_env() -> dict:
    result = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return result


_ENV = _read_env()
TOKEN_X = _ENV.get("INTERNAL_TOKEN", "REPLACE_INTERNAL_TOKEN")


# ── helpers de nodos ───────────────────────────────────────────────────────────

def nid():
    return str(uuid.uuid4())


def cron_node(name, every_minutes):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.cron", "typeVersion": 1,
        "position": [0, 400],
        "parameters": {"triggerTimes": {"item": [
            {"mode": "everyX", "value": every_minutes, "unit": "minutes"}
        ]}}
    }


def trigger_node(name, pos):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.executeWorkflowTrigger", "typeVersion": 1.1,
        "position": pos,
        "parameters": {"inputSource": "passthrough"}
    }


def code_node(name, pos, js):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": pos,
        "parameters": {"jsCode": js, "mode": "runOnceForAllItems"}
    }


def http_post_node(name, pos, url, fields: dict):
    """fields: {field_name: n8n_expression_string}"""
    params = [{"name": k, "value": v} for k, v in fields.items()]
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.4,
        "position": pos,
        "parameters": {
            "url": url, "method": "POST",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Internal-Token", "value": TOKEN_X},
            ]},
            "sendBody": True,
            "contentType": "json",
            "specifyBody": "keypair",
            "bodyParameters": {"parameters": params},
            "options": {}
        }
    }


def http_get_node(name, pos, url):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.4,
        "position": pos,
        "parameters": {
            "url": url, "method": "GET",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Internal-Token", "value": TOKEN_X},
            ]},
            "options": {}
        }
    }


def jina_get_node(name, pos, url):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.4,
        "position": pos,
        "parameters": {"url": url, "method": "GET", "options": {}}
    }


def if_node(name, pos, left_expr, right_value, right_type="boolean", operation="equals"):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.if", "typeVersion": 2.3,
        "position": pos,
        "parameters": {
            "conditions": {
                "options": {
                    "caseSensitive": True, "typeValidation": "strict", "version": 3
                },
                "conditions": [{
                    "id": nid(),
                    "leftValue": left_expr,
                    "rightValue": right_value,
                    "operator": {"type": right_type, "operation": operation,
                                 "rightType": right_type}
                }],
                "combinator": "and"
            },
            "options": {}
        }
    }


def merge_node(name, pos, number=2):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.merge", "typeVersion": 3,
        "position": pos,
        "parameters": {"mode": "append", "options": {}}
    }


def subwf_node(name, pos, wf_id_placeholder):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1.3,
        "position": pos,
        "parameters": {
            "workflowId": {"__rl": True, "value": wf_id_placeholder, "mode": "id"},
            "workflowInputs": {
                "mappingMode": "defineBelow", "value": {},
                "matchingColumns": [], "schema": [],
                "attemptToConvertTypes": False, "convertFieldsToString": True
            },
            "options": {}
        }
    }


def telegram_node(name, pos):
    return {
        "id": nid(), "name": name,
        "type": "n8n-nodes-base.telegram", "typeVersion": 1.2,
        "position": pos,
        "parameters": {
            "chatId":           "={{ $env.TELEGRAM_CHAT_ID }}",
            "text":             "={{ $json.text }}",
            "additionalFields": {}
        },
        "credentials": {
            "telegramApi": {"id": "REPLACE_TELEGRAM_CRED_ID", "name": "Telegram account"}
        }
    }


def conn(connections, src, dst, src_branch=0, dst_branch=0):
    if src not in connections:
        connections[src] = {"main": []}
    while len(connections[src]["main"]) <= src_branch:
        connections[src]["main"].append([])
    connections[src]["main"][src_branch].append(
        {"node": dst, "type": "main", "index": dst_branch}
    )


def make_workflow(name, nodes, connections):
    return {
        "name": name,
        "nodes": nodes,
        "pinData": {},
        "connections": connections,
        "active": False,
        "settings": {"executionOrder": "v1"},
        "versionId": str(uuid.uuid4()),
        "meta": {"templateCredsSetupCompleted": True},
        "tags": []
    }


def save_wf(filename, wf):
    path = os.path.join(BASE, filename)
    with open(path, "w") as f:
        json.dump(wf, f, indent=2, ensure_ascii=False)
    print(f"  OK  {filename}")


# ═══════════════════════════════════════════════════════════════════════════════
# WF-04/05/06  Agentes LLM
# ═══════════════════════════════════════════════════════════════════════════════

PROMPT_TECHNICAL = """
const ctx = $input.first().json;
const t   = ctx.technical || {};
const best= ctx.best_pair || "EURUSD";
const price = t.current_price || 0;
const candles = (t.candles_recent || []).map(c =>
  "O:" + c.open + " H:" + c.high + " L:" + c.low + " C:" + c.close
).join(" | ");

const prompt = "[INST] You are a technical analysis expert.\\n" +
  "Symbol: " + best + " | Price: " + price + " | RSI: " + t.rsi + " | Trend: " + t.trend + "\\n" +
  "SMA9: " + t.sma9 + " SMA20: " + t.sma20 + " SMA50: " + t.sma50 + " SMA200: " + t.sma200 + "\\n" +
  "ATR: " + t.atr + " | Support: " + t.support + " | Resistance: " + t.resistance + "\\n" +
  "Recent candles (last 5): " + candles + "\\n" +
  "Analyse ONLY the technical data. Respond ONLY with JSON:\\n" +
  '{"signal":"buy|sell|neutral","confidence":0.0-1.0,"reason":"one sentence"} [/INST]';

return [{ json: { ...ctx, prompt } }];
"""

PROMPT_FUNDAMENTAL = """
const ctx = $input.first().json;
const best  = ctx.best_pair || "EURUSD";
const news  = (ctx.news || []).slice(0,5).map(n => "- " + (n.headline||"") + " [" + (n.impact||"") + "]").join("\\n");
const events= (ctx.calendar || []).slice(0,5).map(e =>
  "- " + (e.event||"") + " (" + (e.currency||"") + ", " + (e.impact||"") + ", forecast:" + (e.forecast||"") + ")"
).join("\\n");

const prompt = "[INST] You are a fundamental analysis expert.\\n" +
  "Symbol: " + best + "\\n" +
  "Recent news:\\n" + (news || "none") + "\\n" +
  "Upcoming events:\\n" + (events || "none") + "\\n" +
  "Respond ONLY with JSON:\\n" +
  '{"signal":"buy|sell|neutral","confidence":0.0-1.0,"reason":"one sentence"} [/INST]';

return [{ json: { ...ctx, prompt } }];
"""

PROMPT_SENTIMENT = """
const ctx = $input.first().json;
const best   = ctx.best_pair || "EURUSD";
const pos    = ctx.positions || {};
const open   = (pos.open    || []).filter(p => p.symbol === best);
const longV  = open.filter(p => p.type === "BUY" ).reduce((s,p) => s + (p.volume||0), 0);
const shortV = open.filter(p => p.type === "SELL").reduce((s,p) => s + (p.volume||0), 0);
const total  = longV + shortV;
const bias   = total > 0 ? ((longV - shortV) / total * 100).toFixed(1) : "0";

const prompt = "[INST] You are a market sentiment expert.\\n" +
  "Symbol: " + best + "\\n" +
  "Open positions: Long " + longV.toFixed(2) + " lots, Short " + shortV.toFixed(2) + " lots\\n" +
  "Sentiment bias: " + bias + "% towards longs\\n" +
  "Extreme positioning (>70% one side) suggests contrarian signal.\\n" +
  "Respond ONLY with JSON:\\n" +
  '{"signal":"buy|sell|neutral","confidence":0.0-1.0,"reason":"one sentence"} [/INST]';

return [{ json: { ...ctx, prompt } }];
"""


def build_agent_wf(wf_name, agent_key, prompt_js):
    body = {"agent": agent_key, "prompt": "={{ $json.prompt }}"}
    nodes = [
        trigger_node("Trigger",    [0,   400]),
        code_node("Build Prompt",  [220, 400], prompt_js),
        http_post_node("Call LLM", [440, 400], FASTAPI + "/v1/llm/signal", body),
    ]
    c = {}
    conn(c, "Trigger",      "Build Prompt")
    conn(c, "Build Prompt", "Call LLM")
    return make_workflow(wf_name, nodes, c)


wf04 = build_agent_wf("WF-04 Agent Technical",   "technical",   PROMPT_TECHNICAL)
wf05 = build_agent_wf("WF-05 Agent Fundamental", "fundamental", PROMPT_FUNDAMENTAL)
wf06 = build_agent_wf("WF-06 Agent Sentiment",   "sentiment",   PROMPT_SENTIMENT)

# ═══════════════════════════════════════════════════════════════════════════════
# WF-03  LLM Agents (orquesta WF-04/05/06 en paralelo)
# ═══════════════════════════════════════════════════════════════════════════════

nodes_03 = [
    trigger_node("Trigger",          [0,   400]),
    subwf_node("Agent Technical",    [220, 240], "REPLACE_WF04_ID"),
    subwf_node("Agent Fundamental",  [220, 400], "REPLACE_WF05_ID"),
    subwf_node("Agent Sentiment",    [220, 560], "REPLACE_WF06_ID"),
    merge_node("Merge Signals",      [460, 400], number=3),
    code_node("Aggregate Signals",   [680, 400],
        "const items = $input.all();\n"
        "const signals = items.map(i => i.json).filter(s => s.agent);\n"
        "return [{ json: { signals } }];"
    ),
]
c03 = {}
conn(c03, "Trigger",         "Agent Technical")
conn(c03, "Trigger",         "Agent Fundamental")
conn(c03, "Trigger",         "Agent Sentiment")
conn(c03, "Agent Technical",   "Merge Signals", dst_branch=0)
conn(c03, "Agent Fundamental", "Merge Signals", dst_branch=1)
conn(c03, "Agent Sentiment",   "Merge Signals", dst_branch=2)
conn(c03, "Merge Signals",   "Aggregate Signals")
wf03 = make_workflow("WF-03 LLM Agents", nodes_03, c03)

# ═══════════════════════════════════════════════════════════════════════════════
# WF-02  Market Data
# ═══════════════════════════════════════════════════════════════════════════════

PARSE_NEWS_JS = (
    'const raw = $input.first().json?.content || $input.first().json?.data || "";\n'
    'const lines = String(raw).split("\\n").filter(l => l.trim().length > 20).slice(0,20);\n'
    'const CURRENCIES = ["EUR","USD","GBP","JPY","CHF"];\n'
    'const news = lines.map(l => ({\n'
    '  headline: l.trim().slice(0,120),\n'
    '  impact:   l.toLowerCase().includes("high") ? "high" : l.toLowerCase().includes("medium") ? "medium" : "low",\n'
    '  currency: CURRENCIES.find(c => l.includes(c)) || ""\n'
    '}));\n'
    'return [{ json: { news } }];'
)

PARSE_CAL_JS = (
    'const raw = $input.first().json?.content || $input.first().json?.data || "";\n'
    'const lines = String(raw).split("\\n").filter(l => l.trim().length > 10).slice(0,15);\n'
    'const CURRENCIES = ["EUR","USD","GBP","JPY","CHF"];\n'
    'const calendar = lines.map(l => ({\n'
    '  event:    l.trim().slice(0,100),\n'
    '  currency: CURRENCIES.find(c => l.includes(c)) || "",\n'
    '  impact:   l.toLowerCase().includes("high") ? "high" : l.toLowerCase().includes("medium") ? "medium" : "low",\n'
    '  time: "", forecast: ""\n'
    '}));\n'
    'return [{ json: { calendar } }];'
)

MERGE_ALL_JS = (
    'const prices   = $("GET Prices").first().json;\n'
    'const pos      = $("GET Positions").first().json;\n'
    'const positions = (pos.open || []).concat(pos.pending || []);\n'
    'const newsObj  = $("Parse News").first().json;\n'
    'const calObj   = $("Parse Calendar").first().json;\n'
    'return [{ json: { prices, positions, news: newsObj.news || [], calendar: calObj.calendar || [] } }];'
)

nodes_02 = [
    trigger_node("Trigger",        [0,    400]),
    http_get_node("GET Prices",    [220,  400], FASTAPI + "/v1/market/prices"),
    http_get_node("GET Positions", [440,  400], FASTAPI + "/v1/market/positions"),
    jina_get_node("GET News",      [660,  400], "https://r.jina.ai/https://www.forexlive.com/feed/news"),
    jina_get_node("GET Calendar",  [880,  400], "https://r.jina.ai/https://www.forexfactory.com/calendar"),
    code_node("Parse News",        [1100, 400], PARSE_NEWS_JS),
    code_node("Parse Calendar",    [1320, 400], PARSE_CAL_JS),
    code_node("Merge All",         [1540, 400], MERGE_ALL_JS),
]
c02 = {}
conn(c02, "Trigger",        "GET Prices")
conn(c02, "GET Prices",     "GET Positions")
conn(c02, "GET Positions",  "GET News")
conn(c02, "GET News",       "GET Calendar")
conn(c02, "GET Calendar",   "Parse News")
conn(c02, "Parse News",     "Parse Calendar")
conn(c02, "Parse Calendar", "Merge All")
wf02 = make_workflow("WF-02 Market Data", nodes_02, c02)

# ═══════════════════════════════════════════════════════════════════════════════
# WF-07  Order Cleanup
# ═══════════════════════════════════════════════════════════════════════════════

nodes_07 = [
    cron_node("Cron Every Hour", 60),
    http_post_node("POST Cleanup", [220, 400], FASTAPI + "/v1/orders/cleanup", {
        "max_age_hours": "48",
    }),
    code_node("Log Result", [440, 400],
        'const r = $input.first().json;\n'
        'console.log("Cleanup:", (r.cancelled||[]).length, "canceladas,", (r.errors||[]).length, "errores");\n'
        'return [{ json: r }];'
    ),
]
c07 = {}
conn(c07, "Cron Every Hour", "POST Cleanup")
conn(c07, "POST Cleanup",    "Log Result")
wf07 = make_workflow("WF-07 Order Cleanup", nodes_07, c07)

# ═══════════════════════════════════════════════════════════════════════════════
# WF-01  Main Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

GEN_CYCLE_JS = (
    'const rand = () => Math.floor(Math.random() * 0xFFFFFFFF).toString(16).padStart(8,"0");\n'
    'const cycle_id = rand() + rand() + "_" + Date.now();\n'
    'return [{ json: { cycle_id, started_at: new Date().toISOString() } }];'
)

LOG_SKIP_JS = (
    'const r = $input.first().json;\n'
    'console.log("Ciclo saltado:", r.reason);\n'
    'return [{ json: { skipped: true, reason: r.reason } }];'
)

LOG_REJECT_JS = (
    'const r = $input.first().json;\n'
    'console.log("Orden rechazada:", r.rejection_reason);\n'
    'return [{ json: { rejected: true, reason: r.rejection_reason } }];'
)

FMT_TG_JS = (
    'const risk = $("POST Risk Evaluate").first().json;\n'
    'const exec = $("POST Order Execute").first().json;\n'
    'const pair = $("POST Analysis Pairs").first().json;\n'
    'const action = (risk.action || "").toUpperCase();\n'
    'const emoji  = action === "BUY" ? "🟢" : "🔴";\n'
    'const text = emoji + " *ORDEN EJECUTADA*\\n"\n'
    '  + "──────────────\\n"\n'
    '  + "Par: `" + pair.best_pair + "`\\n"\n'
    '  + "Tipo: `" + action + "`\\n"\n'
    '  + "Entry: `" + risk.entry + "`\\n"\n'
    '  + "SL: `" + risk.sl + "`\\n"\n'
    '  + "TP: `" + risk.tp + "`\\n"\n'
    '  + "Vol: `" + risk.volume + "` lots\\n"\n'
    '  + "Conf: `" + ((risk.confidence||0)*100).toFixed(0) + "%`\\n"\n'
    '  + "Ticket: `" + exec.mt5_order_id + "`";\n'
    'return [{ json: { text } }];'
)

CID = '$json.cycle_id'

nodes_01 = [
    cron_node("Cron 5min", 5),
    code_node("Generate Cycle ID", [220, 400], GEN_CYCLE_JS),
    subwf_node("Call Market Data", [440, 400], "REPLACE_WF02_ID"),

    code_node("Merge Cycle ID", [660, 400],
        'const market = $input.first().json;\n'
        'const cycleId = $("Generate Cycle ID").first().json.cycle_id;\n'
        'return [{ json: { ...market, cycle_id: cycleId } }];'
    ),

    http_post_node("POST Context Validate", [880, 400],
        FASTAPI + "/v1/context/validate", {
        "cycle_id":  "={{ $json.cycle_id }}",
        "prices":    "={{ $json.prices }}",
        "news":      "={{ $json.news }}",
        "calendar":  "={{ $json.calendar }}",
        "positions": "={{ $json.positions }}",
    }),

    if_node("Is Context Valid?", [880, 400], "={{ $json.valid }}", True, "boolean", "equals"),
    code_node("Log Skip", [1100, 600], LOG_SKIP_JS),

    http_post_node("POST Analysis Pairs", [1100, 400],
        FASTAPI + "/v1/analysis/pairs", {
        "cycle_id": "={{ $('Merge Cycle ID').first().json.cycle_id }}",
        "prices":   "={{ $('Merge Cycle ID').first().json.prices }}",
    }),

    subwf_node("Call LLM Agents", [1320, 400], "REPLACE_WF03_ID"),

    http_post_node("POST Risk Evaluate", [1540, 400],
        FASTAPI + "/v1/risk/evaluate", {
        "cycle_id":   "={{ $('Merge Cycle ID').first().json.cycle_id }}",
        "best_pair":  "={{ $('POST Analysis Pairs').first().json.best_pair }}",
        "price":      "={{ $('POST Analysis Pairs').first().json.price }}",
        "technical":  "={{ $('POST Analysis Pairs').first().json.technical }}",
        "llm_signals":"={{ $('Call LLM Agents').first().json.signals }}",
        "positions":  "={{ $('Merge Cycle ID').first().json.positions }}",
    }),

    if_node("Has Action?", [1760, 400], "={{ $json.action }}", "skip", "string", "notEquals"),
    code_node("Log Skip Action", [1980, 600], LOG_SKIP_JS),

    http_post_node("POST Order Prepare", [1980, 400],
        FASTAPI + "/v1/order/prepare", {
        "cycle_id": "={{ $('Merge Cycle ID').first().json.cycle_id }}",
        "symbol":   "={{ $('POST Analysis Pairs').first().json.best_pair }}",
        "type":     "={{ ($('POST Risk Evaluate').first().json.action || '').toUpperCase() }}",
        "entry":    "={{ $('POST Risk Evaluate').first().json.entry }}",
        "sl":       "={{ $('POST Risk Evaluate').first().json.sl }}",
        "tp":       "={{ $('POST Risk Evaluate').first().json.tp }}",
        "volume":   "={{ $('POST Risk Evaluate').first().json.volume }}",
    }),

    if_node("Is Approved?", [2200, 400], "={{ $json.approved }}", True, "boolean", "equals"),
    code_node("Log Rejection", [2420, 600], LOG_REJECT_JS),

    http_post_node("POST Order Execute", [2420, 400],
        FASTAPI + "/v1/order/execute", {
        "cycle_id": "={{ $('Merge Cycle ID').first().json.cycle_id }}",
    }),

    http_post_node("POST Order Confirm", [2640, 400],
        FASTAPI + "/v1/order/confirm", {
        "cycle_id":     "={{ $('Merge Cycle ID').first().json.cycle_id }}",
        "mt5_order_id": "={{ $json.mt5_order_id }}",
        "status":       "={{ $json.status }}",
        "fill_price":   "={{ $json.fill_price || null }}",
    }),

    http_post_node("POST Audit Log", [2860, 400],
        FASTAPI + "/v1/audit/log", {
        "cycle_id": "={{ $('Merge Cycle ID').first().json.cycle_id }}",
        "event":    "order_executed",
        "data":     '={{ {"best_pair": $(\'POST Analysis Pairs\').first().json.best_pair, "action": $(\'POST Risk Evaluate\').first().json.action, "mt5_order_id": $(\'POST Order Execute\').first().json.mt5_order_id} }}',
    }),

    code_node("Format Telegram",   [3080, 400], FMT_TG_JS),
    telegram_node("Send Telegram", [3300, 400]),
]

c01 = {}
conn(c01, "Cron 5min",             "Generate Cycle ID")
conn(c01, "Generate Cycle ID",     "Call Market Data")
conn(c01, "Call Market Data",      "Merge Cycle ID")
conn(c01, "Merge Cycle ID",        "POST Context Validate")
conn(c01, "POST Context Validate", "Is Context Valid?")
conn(c01, "Is Context Valid?",     "POST Analysis Pairs", src_branch=0)  # TRUE
conn(c01, "Is Context Valid?",     "Log Skip",            src_branch=1)  # FALSE
conn(c01, "POST Analysis Pairs",   "Call LLM Agents")
conn(c01, "Call LLM Agents",       "POST Risk Evaluate")
conn(c01, "POST Risk Evaluate",    "Has Action?")
conn(c01, "Has Action?",           "POST Order Prepare",  src_branch=0)  # TRUE
conn(c01, "Has Action?",           "Log Skip Action",     src_branch=1)  # FALSE
conn(c01, "POST Order Prepare",    "Is Approved?")
conn(c01, "Is Approved?",          "POST Order Execute",  src_branch=0)  # TRUE
conn(c01, "Is Approved?",          "Log Rejection",       src_branch=1)  # FALSE
conn(c01, "POST Order Execute",    "POST Order Confirm")
conn(c01, "POST Order Confirm",    "POST Audit Log")
conn(c01, "POST Audit Log",        "Format Telegram")
conn(c01, "Format Telegram",       "Send Telegram")
wf01 = make_workflow("WF-01 Main Orchestrator", nodes_01, c01)


# ═══════════════════════════════════════════════════════════════════════════════
# Deploy vía API n8n
# ═══════════════════════════════════════════════════════════════════════════════

def _load_ids() -> dict:
    try:
        with open(IDS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_ids(ids: dict):
    with open(IDS_FILE, "w") as f:
        json.dump(ids, f, indent=2)


def _api(method, path, data=None, *, api_key, base_url):
    url = base_url.rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-N8N-API-KEY", api_key)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"n8n {method} {path} → HTTP {e.code}: {e.read().decode()}")


def _patch_subwf_ids(wf, id_map):
    for node in wf["nodes"]:
        if node["type"] == "n8n-nodes-base.executeWorkflow":
            placeholder = node["parameters"]["workflowId"]["value"]
            if placeholder in id_map:
                node["parameters"]["workflowId"]["value"] = id_map[placeholder]
    return wf


def deploy(api_key: str, base_url: str):
    # Verificar conexión
    try:
        _api("GET", "/api/v1/workflows?limit=1", api_key=api_key, base_url=base_url)
    except Exception as e:
        print(f"  ERROR: No se puede conectar a n8n en {base_url}")
        print(f"         {e}")
        sys.exit(1)

    ids = _load_ids()

    # placeholder_map se construye incrementalmente: cada workflow recibe
    # IDs reales de los que ya fueron procesados antes que él.
    placeholder_map = {
        "REPLACE_WF04_ID": ids.get("WF-04 Agent Technical"),
        "REPLACE_WF05_ID": ids.get("WF-05 Agent Fundamental"),
        "REPLACE_WF06_ID": ids.get("WF-06 Agent Sentiment"),
        "REPLACE_WF02_ID": ids.get("WF-02 Market Data"),
        "REPLACE_WF03_ID": ids.get("WF-03 LLM Agents"),
    }

    # (nombre, placeholder_key_propio, objeto_workflow)
    all_wfs = [
        ("WF-04 Agent Technical",   "REPLACE_WF04_ID", wf04),
        ("WF-05 Agent Fundamental", "REPLACE_WF05_ID", wf05),
        ("WF-06 Agent Sentiment",   "REPLACE_WF06_ID", wf06),
        ("WF-02 Market Data",       "REPLACE_WF02_ID", wf02),
        ("WF-07 Order Cleanup",     None,              wf07),
        ("WF-03 LLM Agents",        "REPLACE_WF03_ID", wf03),
        ("WF-01 Main Orchestrator", None,              wf01),
    ]

    print(f"\n  Conectado a {base_url}")

    for wf_name, own_placeholder, wf_obj in all_wfs:
        wf_copy = json.loads(json.dumps(wf_obj))
        wf_copy = _patch_subwf_ids(wf_copy, placeholder_map)

        payload = {
            "name":        wf_copy["name"],
            "nodes":       wf_copy["nodes"],
            "connections": wf_copy["connections"],
            "settings":    wf_copy.get("settings", {}),
        }

        if wf_name in ids:
            n8n_id = ids[wf_name]
            _api("PUT", f"/api/v1/workflows/{n8n_id}", payload, api_key=api_key, base_url=base_url)
            print(f"  UPDATED  {wf_name}  (id: {n8n_id})")
        else:
            result = _api("POST", "/api/v1/workflows", payload, api_key=api_key, base_url=base_url)
            n8n_id = str(result["id"])
            ids[wf_name] = n8n_id
            _save_ids(ids)
            print(f"  CREATED  {wf_name}  (id: {n8n_id})")

        if own_placeholder:
            placeholder_map[own_placeholder] = n8n_id

    print(f"\n  IDs guardados en {IDS_FILE}")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\nGenerando workflows...")
    save_wf("WF-01-main-orchestrator.json",  wf01)
    save_wf("WF-02-market-data.json",        wf02)
    save_wf("WF-03-llm-agents.json",         wf03)
    save_wf("WF-04-agent-technical.json",    wf04)
    save_wf("WF-05-agent-fundamental.json",  wf05)
    save_wf("WF-06-agent-sentiment.json",    wf06)
    save_wf("WF-07-order-cleanup.json",      wf07)
    print("7 workflows generados")

    if "--deploy" in sys.argv or "-d" in sys.argv:
        api_key  = _ENV.get("N8N_API_KEY", "")
        base_url = _ENV.get("N8N_URL", "http://localhost:5678")
        if not api_key:
            print("\nERROR: N8N_API_KEY no encontrada en .env")
            sys.exit(1)
        print("\nDesplegando en n8n...")
        deploy(api_key, base_url)
        print("\nListo.\n")
    else:
        print("\nPara desplegar: python3 generate.py --deploy\n")
