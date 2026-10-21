//+------------------------------------------------------------------+
//| ListPositionsMagic.mq5 — diagnóstico READ-ONLY                   |
//|                                                                  |
//| Imprime ticket, símbolo, magic, volumen y tipo de cada posición. |
//| No abre, no cierra y no modifica ninguna posición.               |
//|                                                                  |
//| Uso: compilar en MetaEditor y ejecutar en cualquier gráfico.     |
//| La salida aparece en la pestaña "Expertos" del terminal.         |
//+------------------------------------------------------------------+
#property copyright "AI Quant Terminal — auditoría"
#property version   "1.0"
#property script_show_inputs

void OnStart()
  {
   int total = PositionsTotal();
   PrintFormat("[ListPositionsMagic] posiciones=%d", total);
   for(int i = total - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      PrintFormat("[ListPositionsMagic] ticket=%I64u symbol=%s magic=%I64d volume=%.2f type=%s",
                  ticket,
                  PositionGetString(POSITION_SYMBOL),
                  PositionGetInteger(POSITION_MAGIC),
                  PositionGetDouble(POSITION_VOLUME),
                  (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL"));
     }
  }
//+------------------------------------------------------------------+
