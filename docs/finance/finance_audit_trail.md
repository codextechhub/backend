# finance_audit_trail

The finance module's **own, authoritative, append-only audit log** — deliberately
separate from the platform's central `vs_audit`. Two properties make it the record
of truth for financial actions: success rows commit **in the same transaction** as
the action they describe (a posting can never commit without its audit row), and
rows are **immutable** (updates/deletes raise). A best-effort copy is still mirrored
to central audit so the platform-wide activity view stays complete.

Routes (mounted at `/v1/finance/`): `audit-logs/`, `audit-logs/facets/`.

---

## 1. What it is (and what it is NOT)

- **`FinanceAuditLog`** (`models/gl.py:525`): who did what to which document —
  `action` (~70-value enum), `status` (SUCCESS/FAILED), `target_type/id`,
  `document_number`, `message`, `before`/`after` snapshots, `metadata` bag.
- Written via two helpers (`audit.py`): **`record`** (success — call *inside* the
  action's transaction) and **`record_rejection`** (failure — written *outside* the
  rolled-back transaction, so blocked attempts are durably on the record).

**This does NOT:**
- **Replace the journals.** The ledger itself is the primary trail for
  *transactions*; this log captures the actions *around* them (who posted/reversed,
  rejected attempts, period changes, master-data edits).
- **Depend on central audit.** The mirror (`_mirror_to_central`, `audit.py:22`) is
  best-effort and swallows failures; the row here is the source of truth.
- **Expose `metadata`.** The serializer deliberately omits it (internal ids/request
  context); `before`/`after` are exposed as the human-meaningful field snapshot
  (`serializers.py:1157`).

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `FinanceAuditLog` | `models/gl.py:525` | `entity`, `actor?` (null = system), `action`, `status`, `target_type/id`, `document_number`, `message`, `before/after/metadata` (JSON), `created_at` |

Immutability is enforced in the model (`save()` on an existing row and `delete()`
both raise, `models/gl.py:581`) **and at the DB** (BEFORE UPDATE/DELETE triggers,
migration `0025`), so even ORM-bypassing writes are blocked. Indexed on
`(entity, action)`, `(target_type, target_id)`, `(entity, created_at)`.

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | query params | response |
|---|---|---|---|---|
| `GET /audit-logs/` | `finance.audit.view` | The trail (paginated, newest first) | `action`, `status`, `target_type`, `actor` (user id), `date_from`, `date_to` | paginated `FinanceAuditLogSerializer` |
| `GET /audit-logs/facets/` | `finance.audit.view` | Distinct filter options that actually occur for this entity (actors, target types, actions with labels) | — | `{actors, target_types, actions}` |

## 4. Lifecycle / state machine

Append-only: rows are inserted once (SUCCESS in the action's transaction; FAILED
after rollback) and never change. There is no archival/retention job — the trail
grows forever (§8).

## 5. Calculations

None — the only logic is the facets query, which uses `order_by("action")` before
`.distinct()` to clear the model's default ordering (otherwise `-created_at` leaks
into the SELECT and duplicates codes — a real Django footgun, handled).

## 6. What posting does to the ledger

Nothing — this slice only *observes*. The critical guarantee is transactional
coupling: every posting service calls `record(...)` inside its `@transaction.atomic`
block, so ledger change + audit row share one commit; rejections use
`record_rejection` above the atomic core so the audit survives the rollback (see
`finance_journals_posting` §6).

## 7. Worked example

`GET /audit-logs/?entity=LEKKI&action=JOURNAL_POST_REJECTED&date_from=2026-07-01` →
the durable record of every blocked posting attempt this month, each with the actor,
target journal, and the typed error in `message`/`after`. `facets/` fills the filter
dropdowns with only the actions/actors that exist for this entity.

## 8. Gotchas / known limitations

- ✅ **Immutability is enforced at the DB level too** — migration
  `0025_financeauditlog_immutability_triggers` installs BEFORE UPDATE/DELETE triggers
  (PostgreSQL `RAISE EXCEPTION`; MariaDB `SIGNAL` branch for the legacy fallback), so
  queryset `.update()`/`.delete()` and raw SQL are blocked, not just model `save()`.
  Reversible migration; note a future retention/archival job would need to drop the
  triggers first.
- **No retention/archival** — the table grows unboundedly; fine for years at this
  volume, but worth a policy eventually.
- **The central mirror can silently drop events** (by design — best-effort); never
  reconcile compliance questions against `vs_audit`, always against this table.
- `actor` null means "system/automated" — dashboards should label it, not hide it.

## 9. Permissions & tenant isolation

- One key: `finance.audit.view` — **SENSITIVE** in the seed (the trail exposes
  who-did-what and before/after values).
- Entity-scoped queryset; facets likewise. ✅

## 10. Code map

| File | Responsibility |
|---|---|
| `models/gl.py` | `FinanceAuditLog` (+ immutability) |
| `audit.py` | `record`, `record_rejection`, `_mirror_to_central` |
| `views_ops/audit.py` | `FinanceAuditLogListView`, `FinanceAuditFacetsView` |
| `serializers.py` | `FinanceAuditLogSerializer` (hides `metadata`) |
| `constants.py` | `FinanceAuditAction` (~70 values), `FinanceAuditStatus` |

## 11. Test coverage & gaps

Existing (`FinanceAuditTests`): posting writes the authoritative row in-commit;
rejected posts recorded durably; append-only enforced (save/delete raise).

Worth asserting: 403 without `audit.view`; cross-tenant scoping; facets dedup
(the ordering footgun); the `.update()` bypass (documented, or add a DB guard);
mirror failure doesn't break the action.
