#!/usr/bin/env python3
"""
Confirmación Dinero — Orden 82 (USDCHF SELL)
Aplicar Opción E: SL 12p, TP 24p, lot 0.06, risk 7.80€
"""
import argparse
import psycopg2
from datetime import datetime, timezone

def update_order_82(db_url: str) -> dict:
    """Actualiza orden 82 con geometría confirmada por usuario (Opción E)."""
    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                # Verificar orden existente
                cur.execute("""
                    SELECT id, sl, tp, volume, status 
                    FROM orders 
                    WHERE cycle_id = %s AND status = 'pending'
                """, ('USDCHF_1792679520000',))
                row = cur.fetchone()
                if not row:
                    return {'error': f'No se encontró orden para cycle_id USDCHF_1792679520000'}
                
                # Calcular precios derivados
                pip_size = 0.0001
                sl_price = 0.82195 + (12.0 * pip_size)  # SELL: SL arriba
                tp_price = 0.82195 - (24.0 * pip_size)  # SELL: TP abajo
                sl_rounded = round(sl_price, 5)
                tp_rounded = round(tp_price, 5)
                
                # Nuevos valores
                updates = {
                    'sl': sl_rounded,
                    'tp': tp_rounded,
                    'volume': 0.06,
                    'updated_at': datetime.now(timezone.utc),
                }
                
                # Actualizar
                cur.execute("""
                    UPDATE orders 
                    SET sl = %s, tp = %s, volume = %s, updated_at = %s
                    WHERE cycle_id = %s AND status = 'pending'
                """, (sl_rounded, tp_rounded, 0.06, datetime.now(timezone.utc), 'USDCHF_1792679520000'))
                
                # Auditar cambio
                cur.execute("""
                    INSERT INTO audit_log (cycle_id, event, data, created_at)
                    VALUES (%s, 'order_modified', %s, %s)
                """, ('USDCHF_1792679520000', '{"sl_pips": 12, "tp_pips": 24, "lot": 0.06, "risk_eur": 7.80}', 
                     datetime.now(timezone.utc)))
                
                conn.commit()
                return {
                    'status': 'updated',
                    'cycle_id': 'USDCHF_1792679520000',
                    'new_sl': sl_rounded,
                    'new_tp': tp_rounded,
                    'new_volume': 0.06,
                    'risk_eur': 7.80,
                }
    except Exception as e:
        conn.rollback()
        return {'error': str(e)}
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update order 82 con geometría confirmada")
    parser.add_argument("--db-url", required=True, help="URL de conexión PostgreSQL")
    args = parser.parse_args()
    
    result = update_order_82(args.db_url)
    print(f"Resultado: {result}")
