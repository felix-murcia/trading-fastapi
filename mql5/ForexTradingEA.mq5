//+------------------------------------------------------------------+
//|  ForexTradingEA.mq5                                              |
//|  Servidor TCP para el sistema de trading Forex v4.0              |
//|                                                                  |
//|  Protocolo: JSON delimitado por \n (una línea = un mensaje)      |
//|  Request:  {"cmd":"...","param":...}\n                           |
//|  Response: {"ok":true,"data":{...}}\n                            |
//|             {"ok":false,"error":"..."}\n                         |
//|                                                                  |
//|  El EA escucha en ServerPort. FastAPI conecta como cliente.      |
//|  Solo se acepta una conexión simultánea (FastAPI es el único     |
//|  cliente). Si FastAPI se desconecta, el EA espera reconexión.    |
//+------------------------------------------------------------------+
#property copyright "Forex Trading System v4.0"
#property version   "4.00"
#property description "Servidor TCP para integración con FastAPI"

//--- Parámetros configurables
input int    ServerPort    = 9999;    // Puerto TCP donde escucha el EA
input string HmacSecret    = "";      // Debe coincidir con HMAC_SECRET del .env
input bool   LogRequests   = true;    // Loggear cada request/response en el Journal
input int    MagicNumber   = 40000;   // Magic number para órdenes del sistema v4.0

//--- Constantes internas
#define RECV_BUFFER_SIZE  65536
#define TIMER_INTERVAL_MS 100

//--- Handles de sockets
int    g_server = INVALID_HANDLE;
int    g_client = INVALID_HANDLE;

//--- Buffer de recepción acumulado (para mensajes parciales)
string g_recv_buf = "";


//+------------------------------------------------------------------+
//| OnInit: crea y configura el socket servidor                      |
//+------------------------------------------------------------------+
int OnInit()
{
   g_server = SocketCreate();
   if(g_server == INVALID_HANDLE)
   {
      Print("[EA] ERROR SocketCreate: ", GetLastError());
      return INIT_FAILED;
   }

   if(!SocketBind(g_server, ServerPort))
   {
      Print("[EA] ERROR SocketBind puerto ", ServerPort, ": ", GetLastError());
      SocketClose(g_server);
      return INIT_FAILED;
   }

   if(!SocketListen(g_server, 1))
   {
      Print("[EA] ERROR SocketListen: ", GetLastError());
      SocketClose(g_server);
      return INIT_FAILED;
   }

   Print("[EA] Servidor TCP activo en puerto ", ServerPort);
   EventSetMillisecondTimer(TIMER_INTERVAL_MS);
   return INIT_SUCCEEDED;
}


//+------------------------------------------------------------------+
//| OnDeinit: cierra todos los sockets                               |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_client != INVALID_HANDLE) { SocketClose(g_client); g_client = INVALID_HANDLE; }
   if(g_server != INVALID_HANDLE) { SocketClose(g_server); g_server = INVALID_HANDLE; }
   Print("[EA] Sockets cerrados, EA detenido");
}


//+------------------------------------------------------------------+
//| OnTimer: bucle principal — aceptar conexión + leer datos         |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Aceptar nueva conexión si no hay cliente
   if(g_client == INVALID_HANDLE)
   {
      string addr = "";
      g_client = SocketAccept(g_server, addr);
      if(g_client != INVALID_HANDLE)
      {
         Print("[EA] FastAPI conectado desde ", addr);
         g_recv_buf = "";
      }
      return;   // Esperar al próximo tick para leer datos
   }

   // Leer datos disponibles (no bloqueante: timeout=0)
   uchar buf[];
   ArrayResize(buf, RECV_BUFFER_SIZE);
   uint n = SocketRead(g_client, buf, RECV_BUFFER_SIZE, 0);

   if(n > 0)
   {
      g_recv_buf += CharArrayToString(buf, 0, (int)n, CP_UTF8);
      ProcessBuffer();
      return;
   }

   // Si hay error real (no solo "sin datos"), cerrar conexión
   int err = GetLastError();
   if(err != 0 && err != 5274)  // 5274 = ERR_NETSOCKET_TIMEOUT (sin datos disponibles)
   {
      Print("[EA] Cliente desconectado (err=", err, "). Esperando reconexión.");
      SocketClose(g_client);
      g_client    = INVALID_HANDLE;
      g_recv_buf  = "";
   }
}


//+------------------------------------------------------------------+
//| Extrae y procesa mensajes completos del buffer acumulado         |
//+------------------------------------------------------------------+
void ProcessBuffer()
{
   while(true)
   {
      int nl = StringFind(g_recv_buf, "\n");
      if(nl < 0) break;                              // Mensaje incompleto: esperar más datos

      string line   = StringSubstr(g_recv_buf, 0, nl);
      g_recv_buf    = StringSubstr(g_recv_buf, nl + 1);

      line = StringTrimLeft(StringTrimRight(line));
      if(StringLen(line) == 0) continue;

      string resp = HandleCommand(line);
      SendLine(resp);
   }
}


//+------------------------------------------------------------------+
//| Envía una línea JSON terminada en \n al cliente                  |
//+------------------------------------------------------------------+
void SendLine(const string &resp)
{
   string msg = resp + "\n";
   int    len = StringLen(msg);
   uchar  buf[];
   StringToCharArray(msg, buf, 0, len, CP_UTF8);
   ArrayResize(buf, len);   // eliminar null terminator
   SocketSend(g_client, buf, len);

   if(LogRequests)
      Print("[EA] TX: ", resp);
}


//+------------------------------------------------------------------+
//| Router: despacha el comando al handler correspondiente           |
//+------------------------------------------------------------------+
string HandleCommand(const string &json)
{
   if(LogRequests)
      Print("[EA] RX: ", json);

   string cmd = JsonStr(json, "cmd");

   if(cmd == "get_account")    return CmdGetAccount();
   if(cmd == "get_prices")     return CmdGetPrices();
   if(cmd == "get_candles")    return CmdGetCandles(json);
   if(cmd == "get_positions")  return CmdGetPositions();
   if(cmd == "place_order")    return CmdPlaceOrder(json);
   if(cmd == "get_order")      return CmdGetOrder(json);
   if(cmd == "cancel_order")   return CmdCancelOrder(json);

   return Err("unknown_command:" + cmd);
}


//+------------------------------------------------------------------+
//| get_account → equity, balance, currency                          |
//+------------------------------------------------------------------+
string CmdGetAccount()
{
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   string cur = AccountInfoString(ACCOUNT_CURRENCY);

   return Ok("{\"equity\":"  + D(eq, 2) +
             ",\"balance\":" + D(bal, 2) +
             ",\"currency\":\"" + cur + "\"}");
}


//+------------------------------------------------------------------+
//| get_prices → bid de EURUSD, GBPUSD, USDJPY, USDCHF              |
//+------------------------------------------------------------------+
string CmdGetPrices()
{
   string syms[] = {"EURUSD","GBPUSD","USDJPY","USDCHF"};
   int    digs[] = {5, 5, 3, 5};
   string out = "{";
   for(int i = 0; i < 4; i++)
   {
      double bid = SymbolInfoDouble(syms[i], SYMBOL_BID);
      if(i > 0) out += ",";
      out += "\"" + syms[i] + "\":" + D(bid, digs[i]);
   }
   return Ok(out + "}");
}


//+------------------------------------------------------------------+
//| get_candles → array OHLCV del par/timeframe/count solicitado     |
//+------------------------------------------------------------------+
string CmdGetCandles(const string &json)
{
   string sym = JsonStr(json, "symbol");
   string tfs = JsonStr(json, "timeframe");
   int    cnt = (int)JsonNum(json, "count");

   if(sym == "" || cnt <= 0)
      return Err("missing_params:symbol_or_count");

   ENUM_TIMEFRAMES tf = StrToTF(tfs);

   MqlRates rates[];
   int n = CopyRates(sym, tf, 0, cnt, rates);
   if(n <= 0)
      return Err("no_data:" + sym + "_" + tfs);

   // Determinar decimales del símbolo
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   string arr = "[";
   for(int i = 0; i < n; i++)
   {
      if(i > 0) arr += ",";
      arr += "{\"time\":"   + I(rates[i].time) +
             ",\"open\":"   + D(rates[i].open,  digits) +
             ",\"high\":"   + D(rates[i].high,  digits) +
             ",\"low\":"    + D(rates[i].low,   digits) +
             ",\"close\":"  + D(rates[i].close, digits) +
             ",\"volume\":" + I(rates[i].tick_volume) + "}";
   }
   arr += "]";

   return Ok("{\"candles\":" + arr + "}");
}


//+------------------------------------------------------------------+
//| get_positions → posiciones abiertas y órdenes pendientes         |
//+------------------------------------------------------------------+
string CmdGetPositions()
{
   // Posiciones abiertas
   string open_a = "[";
   int pos_n = PositionsTotal();
   for(int i = 0; i < pos_n; i++)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;

      string sym  = PositionGetString(POSITION_SYMBOL);
      string typ  = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      double vol  = PositionGetDouble(POSITION_VOLUME);
      double opx  = PositionGetDouble(POSITION_PRICE_OPEN);
      int    digs = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

      if(i > 0) open_a += ",";
      open_a += "{\"ticket\":"     + I((long)tk) +
                ",\"symbol\":\""   + sym + "\"" +
                ",\"type\":\""     + typ + "\"" +
                ",\"volume\":"     + D(vol, 2) +
                ",\"open_price\":" + D(opx, digs) + "}";
   }
   open_a += "]";

   // Órdenes pendientes
   string pend_a = "[";
   int ord_n = OrdersTotal();
   for(int i = 0; i < ord_n; i++)
   {
      ulong tk = OrderGetTicket(i);
      if(tk == 0) continue;

      string sym  = OrderGetString(ORDER_SYMBOL);
      int    otyp = (int)OrderGetInteger(ORDER_TYPE);
      double vol  = OrderGetDouble(ORDER_VOLUME_CURRENT);
      double opx  = OrderGetDouble(ORDER_PRICE_OPEN);
      int    digs = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

      if(i > 0) pend_a += ",";
      pend_a += "{\"ticket\":"     + I((long)tk) +
                ",\"symbol\":\""   + sym + "\"" +
                ",\"type\":\""     + OrdTypeStr(otyp) + "\"" +
                ",\"volume\":"     + D(vol, 2) +
                ",\"open_price\":" + D(opx, digs) + "}";
   }
   pend_a += "]";

   return Ok("{\"open\":" + open_a + ",\"pending\":" + pend_a + "}");
}


//+------------------------------------------------------------------+
//| place_order → coloca orden pendiente tras verificar HMAC         |
//+------------------------------------------------------------------+
string CmdPlaceOrder(const string &json)
{
   // 1. Verificar firma HMAC usando _canonical (evita problemas de
   //    serialización de floats entre Python y MQL5)
   if(HmacSecret != "" && !VerifyHmac(json))
      return Err("invalid_hmac_signature");

   string sym = JsonStr(json, "symbol");
   string typ = JsonStr(json, "type");
   double px  = JsonNum(json, "price");
   double sl  = JsonNum(json, "sl");
   double tp  = JsonNum(json, "tp");
   double vol = JsonNum(json, "volume");

   if(sym == "" || typ == "" || px == 0 || vol == 0)
      return Err("missing_order_params");

   // 2. Normalizar precio al número de dígitos del símbolo
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   px  = NormalizeDouble(px,  digits);
   sl  = NormalizeDouble(sl,  digits);
   tp  = NormalizeDouble(tp,  digits);
   vol = NormalizeDouble(vol, 2);

   // 3. Construir y enviar la orden
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};

   req.action    = TRADE_ACTION_PENDING;
   req.symbol    = sym;
   req.volume    = vol;
   req.price     = px;
   req.sl        = sl;
   req.tp        = tp;
   req.deviation = 10;
   req.magic     = MagicNumber;
   req.comment   = "v4.0";
   req.type_time = ORDER_TIME_GTC;

   if(typ == "BUY")
   {
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      req.type = (px < ask) ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_BUY_STOP;
   }
   else if(typ == "SELL")
   {
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      req.type = (px > bid) ? ORDER_TYPE_SELL_LIMIT : ORDER_TYPE_SELL_STOP;
   }
   else
      return Err("unknown_type:" + typ);

   if(!OrderSend(req, res))
      return Err("order_send_failed:retcode=" + I(res.retcode));

   if(res.retcode != TRADE_RETCODE_PLACED && res.retcode != TRADE_RETCODE_DONE)
      return Err("broker_rejected:retcode=" + I(res.retcode) + "_" + res.comment);

   Print("[EA] Orden colocada: ", typ, " ", sym, " @ ", px,
         " SL=", sl, " TP=", tp, " ticket=", res.order);

   return Ok("{\"ticket\":" + I((long)res.order) + "}");
}


//+------------------------------------------------------------------+
//| get_order → estado de una orden por ticket                       |
//+------------------------------------------------------------------+
string CmdGetOrder(const string &json)
{
   ulong tk = (ulong)JsonNum(json, "ticket");
   if(tk == 0) return Err("missing_ticket");

   // Buscar en órdenes pendientes activas
   if(OrderSelect(tk))
   {
      string sym  = OrderGetString(ORDER_SYMBOL);
      int    otyp = (int)OrderGetInteger(ORDER_TYPE);
      double vol  = OrderGetDouble(ORDER_VOLUME_CURRENT);
      double px   = OrderGetDouble(ORDER_PRICE_OPEN);
      double sl   = OrderGetDouble(ORDER_SL);
      double tp   = OrderGetDouble(ORDER_TP);
      int    digs = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

      return Ok("{\"ticket\":"   + I((long)tk) +
                ",\"symbol\":\"" + sym + "\"" +
                ",\"type\":\""   + OrdTypeStr(otyp) + "\"" +
                ",\"price\":"    + D(px, digs) +
                ",\"sl\":"       + D(sl, digs) +
                ",\"tp\":"       + D(tp, digs) +
                ",\"volume\":"   + D(vol, 2) +
                ",\"state\":\"pending\"}");
   }

   // Buscar en historial
   if(HistoryOrderSelect(tk))
   {
      int ord_state = (int)HistoryOrderGetInteger(tk, ORDER_STATE);
      string state_str;
      switch(ord_state)
      {
         case ORDER_STATE_FILLED:   state_str = "filled";    break;
         case ORDER_STATE_CANCELED: state_str = "cancelled"; break;
         case ORDER_STATE_EXPIRED:  state_str = "cancelled"; break;
         default:                   state_str = "unknown";   break;
      }
      return Ok("{\"ticket\":" + I((long)tk) + ",\"state\":\"" + state_str + "\"}");
   }

   return Err("order_not_found:" + I((long)tk));
}


//+------------------------------------------------------------------+
//| cancel_order → cancela una orden pendiente por ticket            |
//+------------------------------------------------------------------+
string CmdCancelOrder(const string &json)
{
   ulong tk = (ulong)JsonNum(json, "ticket");
   if(tk == 0) return Err("missing_ticket");

   if(!OrderSelect(tk))
      return Err("order_not_found:" + I((long)tk));

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action = TRADE_ACTION_REMOVE;
   req.order  = tk;

   if(!OrderSend(req, res))
      return Err("cancel_failed:retcode=" + I(res.retcode));

   Print("[EA] Orden cancelada: ticket=", tk);
   return Ok("{\"cancelled\":true,\"ticket\":" + I((long)tk) + "}");
}


//+------------------------------------------------------------------+
//| Verificación HMAC-SHA256                                         |
//|                                                                  |
//| FastAPI incluye "_canonical" (el JSON exacto que fue firmado)   |
//| y "_sig" (HMAC-SHA256 hex de ese canonical).                     |
//| El EA extrae _canonical, calcula HMAC y compara con _sig.        |
//| Esto evita problemas de serialización de floats.                 |
//+------------------------------------------------------------------+
bool VerifyHmac(const string &json)
{
   string canonical = JsonStr(json, "_canonical");
   string received  = JsonStr(json, "_sig");

   if(canonical == "" || received == "")
   {
      Print("[EA] HMAC: faltan campos _canonical o _sig");
      return false;
   }

   string computed = HmacSha256Hex(HmacSecret, canonical);

   if(computed != received)
   {
      Print("[EA] HMAC inválido. Esperado=", computed, " Recibido=", received);
      return false;
   }
   return true;
}


//+------------------------------------------------------------------+
//| HMAC-SHA256 → devuelve hex string de 64 caracteres               |
//|                                                                  |
//| Implementación manual usando CryptEncode(CRYPT_HASH_SHA256):     |
//| HMAC(K,m) = SHA256((K'⊕opad) ∥ SHA256((K'⊕ipad) ∥ m))          |
//| ipad = 0x36×64,  opad = 0x5C×64                                  |
//+------------------------------------------------------------------+
string HmacSha256Hex(const string &key_str, const string &msg_str)
{
   // Convertir strings a bytes (sin null terminator)
   uchar key[], msg[];
   StringToCharArray(key_str, key, 0, StringLen(key_str), CP_UTF8);
   StringToCharArray(msg_str, msg, 0, StringLen(msg_str), CP_UTF8);
   ArrayResize(key, StringLen(key_str));
   ArrayResize(msg, StringLen(msg_str));

   // Si la clave supera el tamaño de bloque SHA256 (64 bytes), hashearla
   uchar actual_key[];
   uchar empty[];
   if(ArraySize(key) > 64)
      CryptEncode(CRYPT_HASH_SHA256, key, empty, actual_key);
   else
      ArrayCopy(actual_key, key);

   // Pad con ceros hasta 64 bytes
   int klen = ArraySize(actual_key);
   ArrayResize(actual_key, 64);
   for(int i = klen; i < 64; i++) actual_key[i] = 0;

   // Construir k_ipad y k_opad
   uchar k_ipad[64], k_opad[64];
   for(int i = 0; i < 64; i++)
   {
      k_ipad[i] = actual_key[i] ^ 0x36;
      k_opad[i] = actual_key[i] ^ 0x5C;
   }

   // inner = SHA256(k_ipad ∥ msg)
   int mlen = ArraySize(msg);
   uchar inner_in[], inner[];
   ArrayResize(inner_in, 64 + mlen);
   ArrayCopy(inner_in, k_ipad, 0, 0, 64);
   ArrayCopy(inner_in, msg,    64, 0, mlen);
   CryptEncode(CRYPT_HASH_SHA256, inner_in, empty, inner);

   // outer = SHA256(k_opad ∥ inner)
   int ilen = ArraySize(inner);
   uchar outer_in[], result[];
   ArrayResize(outer_in, 64 + ilen);
   ArrayCopy(outer_in, k_opad, 0, 0, 64);
   ArrayCopy(outer_in, inner,  64, 0, ilen);
   CryptEncode(CRYPT_HASH_SHA256, outer_in, empty, result);

   // Convertir resultado a hex
   string hex = "";
   for(int i = 0; i < ArraySize(result); i++)
      hex += StringFormat("%02x", (uint)result[i]);

   return hex;
}


//+------------------------------------------------------------------+
//| Helpers de construcción JSON                                     |
//+------------------------------------------------------------------+

string Ok(const string &data)  { return "{\"ok\":true,\"data\":"  + data + "}"; }
string Err(const string &msg)  { Print("[EA] ERROR: ", msg); return "{\"ok\":false,\"error\":\"" + msg + "\"}"; }
string D(double v, int d)      { return DoubleToString(v, d); }
string I(long v)               { return IntegerToString(v); }


//+------------------------------------------------------------------+
//| Parseo JSON mínimo (objetos planos sin anidamiento)              |
//+------------------------------------------------------------------+

// Extrae valor string de "key":"value"
string JsonStr(const string &json, const string &key)
{
   // Buscar "key":"  (con o sin espacio tras :)
   string pat = "\"" + key + "\":\"";
   int pos = StringFind(json, pat);
   if(pos < 0) { pat = "\"" + key + "\": \""; pos = StringFind(json, pat); }
   if(pos < 0) return "";
   pos += StringLen(pat);
   int end = StringFind(json, "\"", pos);
   if(end < 0) return "";

   // Manejar secuencias de escape básicas (\", \\)
   string val = "";
   for(int i = pos; i < end; )
   {
      string ch = StringSubstr(json, i, 1);
      if(ch == "\\" && i + 1 < end)
      {
         string nx = StringSubstr(json, i + 1, 1);
         if(nx == "\"" || nx == "\\") { val += nx; i += 2; continue; }
      }
      val += ch;
      i++;
   }
   return val;
}

// Extrae valor numérico de "key":number
double JsonNum(const string &json, const string &key)
{
   string pat = "\"" + key + "\":";
   int pos = StringFind(json, pat);
   if(pos < 0) { pat = "\"" + key + "\": "; pos = StringFind(json, pat); }
   if(pos < 0) return 0.0;
   pos += StringLen(pat);
   while(pos < StringLen(json) && StringSubstr(json, pos, 1) == " ") pos++;

   string num = "";
   for(int i = pos; i < StringLen(json); i++)
   {
      string ch = StringSubstr(json, i, 1);
      if(ch == "," || ch == "}" || ch == "]" || ch == " " || ch == "\n" || ch == "\r") break;
      num += ch;
   }
   return StringToDouble(num);
}


//+------------------------------------------------------------------+
//| Utilidades                                                       |
//+------------------------------------------------------------------+

ENUM_TIMEFRAMES StrToTF(const string &s)
{
   if(s == "M1")  return PERIOD_M1;
   if(s == "M5")  return PERIOD_M5;
   if(s == "M15") return PERIOD_M15;
   if(s == "M30") return PERIOD_M30;
   if(s == "H1")  return PERIOD_H1;
   if(s == "H4")  return PERIOD_H4;
   if(s == "D1")  return PERIOD_D1;
   return PERIOD_H1;
}

string OrdTypeStr(int t)
{
   switch(t)
   {
      case ORDER_TYPE_BUY:        return "BUY";
      case ORDER_TYPE_SELL:       return "SELL";
      case ORDER_TYPE_BUY_LIMIT:  return "BUY_LIMIT";
      case ORDER_TYPE_SELL_LIMIT: return "SELL_LIMIT";
      case ORDER_TYPE_BUY_STOP:   return "BUY_STOP";
      case ORDER_TYPE_SELL_STOP:  return "SELL_STOP";
      default:                    return "UNKNOWN";
   }
}
