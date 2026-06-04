# API Endpoints Specification - WF-02 Market Data

## Actualizado: 2026-06-04

---

## 1. GET Prices

**Endpoint:** `http://127.0.0.1:8090/v1/market/prices`

**Method:** GET

**Response Structure:**
```json
{
  "EURUSD": 1.16212,
  "GBPUSD": 1.34364,
  "USDJPY": 159.882,
  "USDCHF": 0.79018,
  "XAUUSD": 4466.64
}
```

**Key Points:**
- Devuelve un objeto simple con pares de divisas como keys y precios como values
- No es un array, es un objeto plano

---

## 2. GET Positions

**Endpoint:** `http://127.0.0.1:8090/v1/market/positions`

**Method:** GET

**Response Structure:**
```json
{
  "open": [],
  "pending": []
}
```

**Key Points:**
- Devuelve un objeto con dos campos: `open` y `pending`
- Ambos son arrays (pueden estar vacíos)

---

## 3. GET News (via Jina)

**Endpoint:** `https://r.jina.ai/https://www.forexlive.com/feed/news`

**Method:** GET

**Response Structure:**
```json
[
  {
    "data": "Title: Investinglive RSS Breaking news Feed\n\nURL Source: https://www.forexlive.com/feed/news\n\nMarkdown Content:\n# Investinglive RSS Breaking news Feed\n\n### [texto del titular]\n\n[https://investinglive.com/news/...]\n\nThu, 04 Jun 2026 09:30:11 GMT\n\n### [otro titular]\n\n[https://...]\n\nThu, 04 Jun 2026 09:00:17 GMT\n\n..."
  }
]
```

**Key Points:**
- Devuelve un **array con UN objeto**
- El objeto tiene un campo `data` (NO `content`)
- El campo `data` contiene **markdown con múltiples noticias** separadas por:
  - `### [titulo/url]` - encabezado markdown del artículo
  - `[https://investinglive.com/news/...]` - URL completa como enlace markdown
  - `Thu, 04 Jun 2026 HH:MM:SS GMT` - timestamp

**Estructura interna de cada noticia:**
```
### [https://investinglive.com/news/TITULO-DEL-ARTICULO/]

[https://investinglive.com/news/TITULO-DEL-ARTICULO/](full-url)

Day, DD Mon YYYY HH:MM:SS GMT
```

---

## 4. GET Calendar (via Jina)

**Endpoint:** `https://r.jina.ai/https://www.forexfactory.com/calendar`

**Method:** GET

**Response Structure:**
```json
[
  {
    "data": "Title: Forex Calendar | Forex Factory\n\nURL Source: https://www.forexfactory.com/calendar\n\nMarkdown Content:\n\n| Date | Time | Currency | Impact | Event | Actual | Forecast | Previous |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n| Sun May 31 | 8:30am | GBP | Yellow | MPC Member Greene Speaks | | | |\n| | | USD | Yellow | FOMC Member Waller Speaks | | | |\n| All Day | NZD | Gray | Bank Holiday | | | | |\n| 7:50pm | JPY | Yellow | Capital Spending q/y | 0.0% | 4.1% | 6.5% |\n| 8:30pm | JPY | Yellow | Final Manufacturing PMI | 54.5 | 54.5 | 54.5 |\n| Mon Jun 1 | 2:00am | EUR | Yellow | German Retail Sales m/m | -0.3% | -0.4% | -0.3% |\n..."
  }
]
```

**Key Points:**
- Devuelve un **array con UN objeto**
- El objeto tiene un campo `data` (NO `content`)
- El campo `data` contiene **markdown con tabla** de eventos
- Formato de tabla: `| Field | Field | Field |`
- Los encabezados son: Date, Time, Currency, Impact, Event, Actual, Forecast, Previous
- Las fechas (Sun May 31, Mon Jun 1, etc.) están como rows en la tabla
- Los valores de Actual/Forecast/Previous son números o porcentajes

**Estructura interna de cada evento:**
```
| Fecha/Hora | Currency | Impact | Event Name | Actual | Forecast | Previous |
```

---

## 5. POST Enrich Market Data

**Endpoint:** `http://127.0.0.1:8090/v1/enrichment/market-data`

**Method:** POST

**Request Body:**
```json
{
  "prices": {
    "EURUSD": 1.16212,
    "GBPUSD": 1.34364,
    "USDJPY": 159.882,
    "USDCHF": 0.79018,
    "XAUUSD": 4466.64
  },
  "positions": [],
  "news": [
    {
      "headline": "...",
      "impact": "low",
      "currency": "",
      "source_url": "https://...",
      "content": "..."
    }
  ],
  "calendar": [
    {
      "event": "...",
      "currency": "",
      "impact": "low",
      "time": "",
      "date": "",
      "utc_offset": "",
      "forecast": "",
      "actual": "",
      "previous": "",
      "source_url": "https://..."
    }
  ]
}
```

**Response Body:**
```json
{
  "prices": { ... },
  "positions": [],
  "news": [
    {
      "headline": "...",
      "currency": "EUR",
      "impact": "high",
      "summary": "...",
      "sentiment": "bullish",
      "source_url": "...",
      "timestamp": "2026-06-04T09:25:07.737113"
    }
  ],
  "calendar": [
    {
      "event": "...",
      "currency": "USD",
      "impact": "high",
      "date": "2026-06-04",
      "time": "8:30am",
      "utc_offset": "UTC",
      "forecast": "54.0",
      "actual": "54.5",
      "previous": "52.7",
      "source_url": "...",
      "timestamp": "2026-06-04T09:25:07.737113"
    }
  ],
  "validation": {
    "is_valid": true,
    "errors": []
  }
}
```

---

## Summary

| Endpoint | Returns | Field con data | Estructura |
|----------|---------|---|---|
| GET Prices | Object | N/A | `{PAIR: price}` |
| GET Positions | Object | N/A | `{open: [], pending: []}` |
| GET News | Array[1] | `.data` | Markdown con noticias separadas por ### y timestamps |
| GET Calendar | Array[1] | `.data` | Markdown tabla con eventos y valores forecast/actual/previous |
| POST Enrich | Object | N/A | Retorna prices, positions, news enriquecido, calendar enriquecido, validation |

---

**Nota:** GET News y GET Calendar devuelven arrays de 1 elemento. El elemento contiene un campo `.data` con TODO el contenido en formato markdown que necesita ser parseado.
