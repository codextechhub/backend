# Finance cost centers

`CostCenter` is the finance module's analytical bucket for slicing revenue,
expenses, budgets, and posted ledger activity by department, project, branch
unit, or any other management-reporting segment. It does **not** replace the
chart of accounts: keep accounts generic, such as `Salaries`, `Transport`, or
`Tuition Income`, and attach a cost center to each transactional line when you
need to answer questions like "how much did Primary spend on salaries?".

## When to use a cost center

Use a cost center when the same ledger account needs to be split across multiple
internal owners:

- `PRI` — Primary School
- `SEC` — Secondary School
- `ADM` — Administration
- `SPORT` — Sports Programme

A journal, invoice, payroll, expense-claim, petty-cash, procurement, or budget
line can then carry the relevant cost center while still posting to the normal
account in the chart of accounts.

## API examples

All finance endpoints are scoped to a `LedgerEntity`, so pass `?entity=<id|code>`
on each request. The examples below assume the caller is authenticated and has
the relevant `finance.costcenter.*` and transaction permissions.

### Create or update a cost center

`POST /v1/finance/cost-centers/?entity=SCHOOL-001`

```bash
curl -X POST "https://api.example.com/v1/finance/cost-centers/?entity=SCHOOL-001" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "code": "PRI",
    "name": "Primary School",
    "is_active": true
  }'
```

Because the view uses `update_or_create`, posting the same `code` again updates
the existing cost center for that ledger entity.

Example response:

```json
{
  "success": true,
  "message": "Cost centre PRI created.",
  "data": {
    "id": 12,
    "code": "PRI",
    "name": "Primary School",
    "parent_id": null,
    "parent_code": null,
    "is_active": true
  }
}
```

### Create a child cost center

Use `parent` as either the parent cost center code or id.

```bash
curl -X POST "https://api.example.com/v1/finance/cost-centers/?entity=SCHOOL-001" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "code": "PRI-Y1",
    "name": "Primary Year 1",
    "parent": "PRI"
  }'
```

### List cost centers

```bash
curl "https://api.example.com/v1/finance/cost-centers/?entity=SCHOOL-001&is_active=true" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

The optional `is_active=true|false` query parameter filters active or inactive
cost centers.

## Attaching a cost center to transactions

Transaction endpoints that accept line items resolve `cost_center` by code first,
then by numeric id. Omitting `cost_center` leaves that line unallocated.

### Manual invoice line

```json
{
  "customer": "CUST-0001",
  "invoice_date": "2026-06-26",
  "reference": "INV-DEMO-001",
  "lines": [
    {
      "revenue_account": "4000",
      "description": "Primary tuition fees",
      "quantity": "1",
      "unit_price": 25000000,
      "cost_center": "PRI"
    }
  ]
}
```

When the invoice is posted, the generated journal line keeps the same cost
center, allowing account activity and reports to show the revenue against `PRI`.

### Expense claim line

```json
{
  "claimant": "EMP-001",
  "claim_date": "2026-06-26",
  "lines": [
    {
      "expense_account": "6100",
      "description": "Classroom teaching supplies",
      "amount": 150000,
      "cost_center": "PRI"
    }
  ]
}
```

### Budget line

```json
{
  "account": "6100",
  "period_no": 1,
  "amount": 5000000,
  "cost_center": "PRI"
}
```

This records the period budget for account `6100` specifically against the
Primary School cost center.

## Django ORM example

Use the ORM directly from application services or management commands when you
are not going through the REST API:

```python
from vs_finance.models import Account, CostCenter, LedgerEntity

entity = LedgerEntity.objects.get(code="SCHOOL-001")

primary, _ = CostCenter.objects.update_or_create(
    entity=entity,
    code="PRI",
    defaults={"name": "Primary School", "is_active": True},
)

salaries = Account.objects.get(entity=entity, code="6100")

# Later, when creating a model that has a cost_center ForeignKey:
line.cost_center = primary
line.account = salaries
line.save(update_fields=["cost_center", "account"])
```

## Practical guidance

- Keep cost center codes short, stable, and unique per ledger entity.
- Do not create separate chart-of-account records just to represent departments;
  use one account plus multiple cost centers instead.
- Parent cost centers are useful for rollups such as `PRI` containing `PRI-Y1`,
  `PRI-Y2`, and `PRI-Y3`.
- Use `is_active=false` to stop new allocations without deleting historical
  references.
