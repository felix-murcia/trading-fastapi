"""Fronteras de error declaradas (H-K, 2026-10-22).

La captura genérica borraba la distinción entre una caída EXTERNA (DB, HTTP,
JSON, modelo) y un ERROR DE PROGRAMACIÓN (`AttributeError`, `NameError`,
`ImportError`, `AssertionError`). Estas tuplas conservan la degradación
declarada en cada frontera y dejan propagar los bugs: un `AttributeError` ya
no se convierte en "sin datos".

No es un alias de `Exception`: cada nombre documenta qué familia de fallos
puede degradarse. El boundary elige la tupla más estrecha que cubra sus
llamadas; `LOOP_ERRORS` es sólo para los bucles de fondo que tocan varias
fronteras y no deben morir por una caída transitoria.
"""
import json

import asyncpg
import httpx

# PostgreSQL: errores del servidor + fallo de interfaz/conexión + socket.
# `RuntimeError` es el contrato explícito de `db.connection.get_pool()` cuando
# el pool aún no existe (arranque/suite); no es un comodín para bugs.
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, RuntimeError)

# Cliente HTTP (MT5 MCP, Telegram, news, GCS por HTTP…): red y status.
HTTP_ERRORS = (httpx.HTTPError, OSError)

# Payloads externos: JSON inválido, campos ausentes, tipos inesperados.
PARSE_ERRORS = (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError)

# Artefactos de modelo (SB3/zip/npz): IO, formato y estado.
MODEL_ERRORS = (OSError, ValueError, RuntimeError, KeyError)

# Bucle de fondo que cruza DB + HTTP + parseo + modelo. Sigue sin capturar
# errores de programación (AttributeError/NameError/ImportError/AssertionError).
LOOP_ERRORS = DB_ERRORS + HTTP_ERRORS + PARSE_ERRORS + MODEL_ERRORS
