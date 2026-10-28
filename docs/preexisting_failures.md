# Fallos preexistentes de la suite (Sesión 0)

Línea base: `6 failed, 1443 passed, 2 xfailed` sobre `master@a3cee7f`
(pytest completo, ver `docs/baseline_pytest.txt`).

Estos fallos **existen antes** de la refactorización hexagonal y no se
arreglan en esta sesión. Se listan solo para que las sesiones siguientes
puedan distinguir una regresión real de la deuda heredada.

| # | Test | Causa |
|---|------|-------|
| 1 | `tests/test_architecture_rules.py::test_complejidad_no_crece` | `services/order_manager.py::mark_order_filled` mide 85 líneas y no está en `tests/architecture_baseline.py::BIG_FUNCS` (umbral 80). |
| 2 | `tests/test_architecture_rules.py::test_except_exception_no_crece` | `routers/ai.py` tiene 1 `except Exception` y `EXCEPT_EXCEPTION` está vacío. |
| 3 | `tests/test_ea_close_outbox.py::TestCloseOutbox::test_ea_version_bumped_to_14_3` | El test exige `#property version "14.3"`; el EA declara `15.2`. |
| 4 | `tests/test_ea_poll_offset.py::TestVersionBump::test_version_14_3_in_property_and_define` | Misma causa: el test espera `14.3`, el EA está en `15.2`. |
| 5 | `tests/test_g3_one_position_per_pair.py::TestEaPositionCounting::test_ea_version_was_bumped` | Misma causa: `#property version   "14.3"` no aparece en `mql5/AI_Quant_Terminal.mq5`. |
| 6 | `tests/test_g3_swap_and_geometry_audit.py::TestSwapIsReported::test_ea_sends_the_swap` | Misma causa: la aserción de versión `14.3` corre antes que la de `swap`. |

## Nota sobre el fallo #2

La Tarea 0.2 de esta sesión elimina el `except Exception` de
`routers/ai.py:2392`, que es la causa exacta del fallo #2. Por construcción,
ese test pasa a verde con el cambio y la suite queda en **5 fallos
preexistentes** (decisión registrada con Félix en esta sesión). Los fallos
3-6 comparten una única causa: los tests de versión del EA se quedaron en
`14.3` mientras el EA va por `15.2`.
