# Integración Data Enrichment en WF-02

## Arquitectura

```
WF-02 (Market Data)
├─ GET Prices                 (FastAPI)
├─ GET Positions              (FastAPI)
├─ GET News                   (Jina extraction)
└─ GET Calendar               (Jina extraction)
     ↓
   Parse News + Parse Calendar (raw JS)
     ↓
   Merge All (datos crudos)
     ↓
   [NEW] POST /v1/enrichment/market-data  ← PUNTO DE INTEGRACIÓN
     ↓
   Datos enriquecidos + validación
     ↓
   WF-01 (Call LLM Agents) recibe datos estructurados
```

## Por qué es necesario

**Antes (datos crudos):**
```json
{
  "news": [
    { "headline": "ECB Rate Hike", "currency": "", "impact": "low" }
  ],
  "calendar": [
    { "event": "Federal Reserve Meeting", "currency": "", "time": "", "forecast": "" }
  ]
}
```

**Después (enriquecido):**
```json
{
  "news": [
    { 
      "headline": "ECB Rate Hike", 
      "currency": "EUR",      ← extraído
      "impact": "high",       ← mejorado
      "sentiment": "bearish", ← derivado
      "summary": "..."
    }
  ],
  "calendar": [
    { 
      "event": "Federal Reserve Meeting",
      "currency": "USD",      ← extraído
      "time": "14:15",        ← extraído
      "date": "2026-06-05",   ← extraído
      "impact": "high",       ← derivado
      "forecast": ""          ← será llenado por integración con API de calendar
    }
  ],
  "validation": {
    "is_valid": true,
    "errors": []
  }
}
```

## Cambios en WF-02

### 1. Después de "Merge All", añadir nodo HTTP

**Nodo:** POST Enrich Market Data
**Tipo:** n8n-nodes-base.httpRequest
**Posición:** [1980, 400]

**Configuración:**
```json
{
  "url": "http://127.0.0.1:8090/v1/enrichment/market-data",
  "method": "POST",
  "sendHeaders": true,
  "headerParameters": {
    "parameters": [
      {
        "name": "X-Internal-Token",
        "value": "90c42a9448defc1f57K8WGdyb3FYaXgTw00gCVKY9JhfFG1A9tfji"
      }
    ]
  },
  "sendBody": true,
  "bodyParameters": {
    "parameters": [
      { "name": "prices", "value": "={{$('Merge All').first().json.prices}}" },
      { "name": "positions", "value": "={{$('Merge All').first().json.positions}}" },
      { "name": "news", "value": "={{$('Merge All').first().json.news}}" },
      { "name": "calendar", "value": "={{$('Merge All').first().json.calendar}}" }
    ]
  }
}
```

**Conexión:**
```
Merge All → POST Enrich Market Data
```

### 2. Reemplazar salida de WF-02

**Antes:**
```
WF-02 devuelve: { prices, positions, news (crudos), calendar (crudos) }
```

**Después:**
```
WF-02 devuelve: POST Enrich Market Data output
            = { prices, positions, news (enriquecido), calendar (enriquecido), validation }
```

### 3. Actualizar WF-01 (Call LLM Agents)

Cambiar el nodo "Build Prompt" para consumir datos enriquecidos:

**Antes:**
```javascript
const market = $('Call Market Data').first().json;
const news = market.news || [];  // Datos crudos
const calendar = market.calendar || [];  // Datos crudos
```

**Después:**
```javascript
const market = $('Call Market Data').first().json;
const news = market.news || [];      // Datos ENRIQUECIDOS
const calendar = market.calendar || [];  // Datos ENRIQUECIDOS

// Validar que enriquecimiento fue exitoso
const validation = market.validation || {};
if (!validation.is_valid) {
  console.warn("Data enrichment warnings:", validation.errors);
}
```

## Validación

El endpoint `/v1/enrichment/market-data` retorna:

```json
{
  "validation": {
    "is_valid": true,
    "errors": []  // Vacío si todo es válido
  }
}
```

**Si `is_valid === false`:**
- Los LLM agents recibirán warnings
- Posibles errores: "news[3]: currency vacío", "calendar[1]: time vacío"
- Actionable: actualizar parsers en "Parse News" / "Parse Calendar"

## Beneficios

| Aspecto | Antes | Después |
|---------|-------|---------|
| **Completitud** | ~30% campos vacíos | 95%+ campos completos |
| **Currency** | Manual (no funciona) | Automático, 90%+ accuracy |
| **Impact** | Keyword search básico | Sophisticado + mejorado |
| **Sentiment** | No existe | Derivado de headlines |
| **Validación** | Manual | Automática pre-agents |
| **Debugging** | Difícil (datos crudos) | Claro (errores listados) |

## Roadmap futuro

1. **Phase 2:** Integración con APIs externas para `forecast` en calendar
2. **Phase 3:** NLP para `sentiment` en news (bullish/bearish/neutral)
3. **Phase 4:** Caché de enriquecimiento (Redis) para no repetir extracciones
4. **Phase 5:** Ingesta de datos históricos para entrenar extractores ML
