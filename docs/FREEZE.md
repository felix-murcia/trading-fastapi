# Congelar `master` — Sesión 0 (Tarea 0.4)

Repositorio: `felix-murcia/trading-fastapi` (remoto `origin`).
Rama por defecto: **`master`**.

**Este documento no se ha ejecutado.** Los comandos de abajo los lanza Félix
cuando decida congelar la rama. Proteger `master` es un cambio en GitHub, no
en el repo local: no se puede revisar con `git diff` y afecta a todos los
colaboradores y a los workflows.

## Por qué

La decisión de esta sesión es refactorizar a hexagonal **en el mismo repo, con
estrangulamiento progresivo**. Mientras el `legacy` y el dominio nuevo
convivan, `master` debe dejar de aceptar pushes directos: cada paso entra por
PR con la suite en verde.

## Estado de partida que hay que verificar antes

```bash
gh auth status                                   # debe estar autenticado con permiso admin
gh api repos/felix-murcia/trading-fastapi \
  --jq '.default_branch, .permissions.admin'
```

Debe imprimir `master` y `true`. Si imprime `false`, la cuenta no es admin del
repo y el comando siguiente fallará con 403.

## Comando exacto (NO ejecutado por el agente)

```bash
gh api --method PUT \
  repos/felix-murcia/trading-fastapi/branches/master/protection \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks='{"strict":true,"contexts":["suite (pytest)"]}' \
  -f enforce_admins=true \
  -f required_pull_request_reviews='{"dismiss_stale_reviews":true,"require_code_owner_reviews":false,"required_approving_review_count":1}' \
  -f restrictions='null' \
  -F allow_force_pushes=false \
  -F allow_deletions=false \
  -F required_conversation_resolution=true
```

Qué activa, campo por campo:

| Campo | Valor | Efecto |
|-------|-------|--------|
| `required_status_checks.strict` | `true` | la rama del PR debe estar al día con `master` antes de fusionar |
| `required_status_checks.contexts` | `["suite (pytest)"]` | nombre exacto del check (`.github/workflows/tests.yml`, job `pytest`, `name: suite (pytest)`) |
| `enforce_admins` | `true` | ni el propietario empuja directo |
| `required_pull_request_reviews.required_approving_review_count` | `1` | un revisor aprueba; con `enforce_admins` el PR sigue siendo obligatorio |
| `required_pull_request_reviews.dismiss_stale_reviews` | `true` | un push nuevo invalida la aprobación anterior |
| `restrictions` | `null` | sin lista blanca de usuarios (con `enforce_admins` no aporta) |
| `allow_force_pushes` | `false` | prohíbe `git push --force` |
| `allow_deletions` | `false` | prohíbe borrar la rama |
| `required_conversation_resolution` | `true` | no se fusiona con comentarios abiertos |

> Nota sobre `required_approving_review_count: 1`: si Félix trabaja solo, un PR
> propio no puede auto-aprobarse. Alternativas: poner `0` y dejar solo el
> check obligatorio, o usar un segundo colaborador/`dismiss` manual. Elegir una
> antes de lanzar el comando.

## Verificación (después de ejecutarlo)

```bash
gh api repos/felix-murcia/trading-fastapi/branches/master/protection \
  --jq '{checks: .required_status_checks.contexts, admins: .enforce_admins.enabled, force: .allow_force_pushes.enabled, prs: .required_pull_request_reviews.required_approving_review_count}'
```

Salida esperada:

```json
{"checks":["suite (pytest)"],"admins":true,"force":false,"prs":1}
```

## Reversión (si bloquea el trabajo)

```bash
gh api --method DELETE repos/felix-murcia/trading-fastapi/branches/master/protection
```

## Alternativa equivalente: rulesets

GitHub empuja a *rulesets* en lugar de la protección clásica. Mismo efecto,
sintaxis distinta:

```bash
gh api --method POST repos/felix-murcia/trading-fastapi/rulesets \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "name": "freeze-master",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/master"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_linear_history"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true
    }},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "required_status_checks": [{"context": "suite (pytest)"}]
    }}
  ]
}
JSON
```

## Mientras `master` no esté congelado

Las ramas de esta sesión salen de `master` y no se fusionan sin revisión:

- `sesion0/baseline` — baseline de pytest/git y fallos preexistentes
- `sesion0/fix-ai-2392` — `except Exception` en `routers/ai.py`
- `sesion0/fix-ar-963` — `except BaseException` en `services/auto_retrain.py:963`
- `sesion0/fix-ar-968` — `except BaseException` en `services/auto_retrain.py:968`
- `sesion0/docs-rules` — `BUSINESS_RULES.md` + este `FREEZE.md`
