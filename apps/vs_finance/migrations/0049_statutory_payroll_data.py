"""The national payroll data, and the statutory accounts every existing set of books needs.

**National data.** The 2026 Nigerian PAYE table, the 37 states (36 and the FCT)
whose revenue services PAYE is remitted to, and the licensed pension fund
administrators. It is written here as data and maintained afterwards by platform
staff through the platform endpoints; nothing in code reads these figures.

**The figures must be confirmed by an accountant before a tenant relies on them.**
The table follows the Nigeria Tax Act 2025 as it applies from 1 January 2026:
annual income taxed at 0% on the first N800,000, 15% on the next N2,200,000, 18%
on the next N9,000,000, 21% on the next N13,000,000, 23% on the next N25,000,000
and 25% above N50,000,000; pension and NHF contributions deducted in full; rent
relief at 20% of annual rent, at most N500,000. Reliefs the payroll has no input
for (health insurance, life assurance, mortgage interest) are not modelled. The
revenue service names are the conventional "<State> State Internal Revenue
Service" form, and the PFA list was taken from the regulator's published list as
known when this was written; both are to be checked against current names.

**Existing books.** Every entity whose chart carries the PAYE payable (2310)
gains the NHF, NSITF and ITF payables (2340-2360), the employer pension, NSITF
and ITF expenses (5210-5230) and the three obligations that drain the payables,
exactly as a newly seeded chart has them. Payroll lines written before lines
recorded their taxable pay take their gross as it, which is what PAYE was charged
on then, so year-to-date PAYE counts them.

Reversing leaves the national data and the accounts in place: accounts may
already carry postings, and tables may already have priced payroll lines.
"""
from django.db import migrations

NGN = 100  # kobo per naira

STATES = [
    ("AB", "Abia"), ("AD", "Adamawa"), ("AK", "Akwa Ibom"), ("AN", "Anambra"),
    ("BA", "Bauchi"), ("BY", "Bayelsa"), ("BE", "Benue"), ("BO", "Borno"),
    ("CR", "Cross River"), ("DE", "Delta"), ("EB", "Ebonyi"), ("ED", "Edo"),
    ("EK", "Ekiti"), ("EN", "Enugu"), ("GO", "Gombe"), ("IM", "Imo"),
    ("JI", "Jigawa"), ("KD", "Kaduna"), ("KN", "Kano"), ("KT", "Katsina"),
    ("KE", "Kebbi"), ("KO", "Kogi"), ("KW", "Kwara"), ("LA", "Lagos"),
    ("NA", "Nasarawa"), ("NI", "Niger"), ("OG", "Ogun"), ("ON", "Ondo"),
    ("OS", "Osun"), ("OY", "Oyo"), ("PL", "Plateau"), ("RI", "Rivers"),
    ("SO", "Sokoto"), ("TA", "Taraba"), ("YO", "Yobe"), ("ZA", "Zamfara"),
]

PFAS = [
    ("ACCESSARM", "Access ARM Pensions"),
    ("CARDINAL", "CardinalStone Pensions"),
    ("FCMB", "FCMB Pensions"),
    ("FIDELITY", "Fidelity Pension Managers"),
    ("GTPM", "Guaranty Trust Pension Managers"),
    ("LEADWAY", "Leadway Pensure PFA"),
    ("NLPC", "NLPC Pension Fund Administrators"),
    ("NORRENBERG", "Norrenberger Pensions"),
    ("NPF", "NPF Pensions"),
    ("OAK", "OAK Pensions"),
    ("PAL", "Pensions Alliance"),
    ("PREMIUM", "Premium Pension"),
    ("RADIX", "Radix Pension Managers"),
    ("STANBIC", "Stanbic IBTC Pension Managers"),
    ("TANGERINE", "Tangerine APT Pensions"),
    ("TRUSTFUND", "Trustfund Pensions"),
    ("VERITAS", "Veritas Glanvills Pensions"),
]

BANDS_2026 = [
    (0, 800_000), (800_000, 3_000_000), (3_000_000, 12_000_000),
    (12_000_000, 25_000_000), (25_000_000, 50_000_000), (50_000_000, None),
]
RATES_2026 = [0, 1500, 1800, 2100, 2300, 2500]

RELIEFS_2026 = [
    ("PENSION", "Pension contributions", "CONTRIBUTION", "PENSION", 0, None),
    ("NHF", "National Housing Fund contributions", "CONTRIBUTION", "NHF", 0, None),
    ("RENT", "Rent relief", "PERCENT_CAPPED", "ANNUAL_RENT", 2000, 500_000 * NGN),
]

#: (code, name, type, normal balance, ifrs line)
ACCOUNTS = [
    ("2340", "NHF Payable", "LIABILITY", "CREDIT", "EMPLOYEE_PAYABLES", "2000"),
    ("2350", "NSITF Payable", "LIABILITY", "CREDIT", "EMPLOYEE_PAYABLES", "2000"),
    ("2360", "ITF Payable", "LIABILITY", "CREDIT", "EMPLOYEE_PAYABLES", "2000"),
    ("5210", "Employer Pension Contributions", "EXPENSE", "DEBIT", "ADMIN_EXPENSES", "5000"),
    ("5220", "NSITF Contributions", "EXPENSE", "DEBIT", "ADMIN_EXPENSES", "5000"),
    ("5230", "ITF Levy", "EXPENSE", "DEBIT", "ADMIN_EXPENSES", "5000"),
]

#: (code, name, type, liability code, authority, frequency, filing day)
OBLIGATIONS = [
    ("NHF", "National Housing Fund", "NHF", "2340", "Federal Mortgage Bank of Nigeria", "MONTHLY", 30),
    ("NSITF", "NSITF Employee Compensation", "NSITF", "2350",
     "Nigeria Social Insurance Trust Fund", "MONTHLY", 16),
    ("ITF", "ITF Training Levy", "ITF", "2360", "Industrial Training Fund", "ANNUAL", 1),
]


def seed_national_data(apps, schema_editor):
    Jurisdiction = apps.get_model("vs_finance", "PayrollTaxJurisdiction")
    Pfa = apps.get_model("vs_finance", "PensionFundAdministrator")
    Table = apps.get_model("vs_finance", "PayeTaxTable")
    Band = apps.get_model("vs_finance", "PayeTaxBand")
    Relief = apps.get_model("vs_finance", "PayeTaxRelief")

    for code, name in STATES:
        Jurisdiction.objects.get_or_create(
            country="NG", code=code,
            defaults={"name": name, "authority_name": f"{name} State Internal Revenue Service"},
        )
    Jurisdiction.objects.get_or_create(
        country="NG", code="FC",
        defaults={"name": "Federal Capital Territory",
                  "authority_name": "FCT Internal Revenue Service"},
    )
    for code, name in PFAS:
        Pfa.objects.get_or_create(code=code, defaults={"name": name})

    table, created = Table.objects.get_or_create(
        country="NG", tax_year=2026,
        defaults={
            "name": "Nigeria PAYE 2026",
            "source_reference": "Nigeria Tax Act 2025, personal income tax rates from 1 January 2026",
            "notes": "Seeded as data. To be confirmed by an accountant before use.",
        },
    )
    if created:
        Band.objects.bulk_create([
            Band(table=table, sequence=i, lower=lower * NGN,
                 upper=None if upper is None else upper * NGN, rate_bps=rate)
            for i, ((lower, upper), rate) in enumerate(zip(BANDS_2026, RATES_2026))
        ])
        Relief.objects.bulk_create([
            Relief(table=table, sequence=i, code=code, name=name, kind=kind, basis=basis,
                   rate_bps=rate, cap_amount=cap)
            for i, (code, name, kind, basis, rate, cap) in enumerate(RELIEFS_2026)
        ])


def extend_existing_books(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    Obligation = apps.get_model("vs_finance", "TaxObligation")
    PayrollLine = apps.get_model("vs_finance", "PayrollLine")

    entity_ids = Account.objects.filter(code="2310").values_list("entity_id", flat=True)
    for entity_id in entity_ids:
        have = {a.code: a for a in Account.objects.filter(entity_id=entity_id)}
        for code, name, kind, balance, line, parent in ACCOUNTS:
            if code not in have:
                have[code] = Account.objects.create(
                    entity_id=entity_id, code=code, name=name, account_type=kind,
                    normal_balance=balance, ifrs_line=line, is_postable=True,
                    parent=have.get(parent),
                )
        for code, name, kind, liability, authority, frequency, day in OBLIGATIONS:
            Obligation.objects.get_or_create(
                entity_id=entity_id, code=code,
                defaults={
                    "name": name, "obligation_type": kind, "liability_account": have[liability],
                    "authority_name": authority, "frequency": frequency, "filing_day": day,
                },
            )

    from django.db.models import F

    PayrollLine.objects.filter(taxable_pay=0, gross_amount__gt=0).update(taxable_pay=F("gross_amount"))


def keep_on_reverse(apps, schema_editor):
    """Leave everything: the accounts may carry postings and the tables may have priced lines."""
    return None


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0048_statutory_payroll"),
    ]

    operations = [
        migrations.RunPython(seed_national_data, keep_on_reverse),
        migrations.RunPython(extend_existing_books, keep_on_reverse),
    ]
