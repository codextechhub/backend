"""Fill the plain-language purpose of every default account that has none.

Sets of books created without descriptions hold blank ones on the accounts of
the default chart. This writes each account's purpose onto every set of books
whose account has that code and no description. A description a
person wrote is never touched, and reversing leaves the text in place.

The text is a frozen copy of the default chart's purposes, held here so the
migration reads the same whatever the application code says.
"""
from django.db import migrations

PURPOSES = {
    "1000": (
        "The heading for everything the organisation owns. You cannot post "
        "to it; its figure is the total of the accounts under it."
    ),
    "1100": (
        "The money held in your bank accounts and as cash. It goes up when "
        "payers pay you and down when you pay vendors or staff."
    ),
    "1110": (
        "Small cash kept on hand for day-to-day spending. It goes down as "
        "vouchers are paid out and up when the float is topped up from the "
        "bank."
    ),
    "1125": (
        "Online payments the payment provider has confirmed but not yet "
        "paid into your bank. It empties when the provider's settlement "
        "reaches the bank."
    ),
    "1200": (
        "What customers and payers owe you for invoices not yet paid. It "
        "goes up when you issue an invoice and down when a payment or "
        "credit note is applied."
    ),
    "1290": (
        "An estimate of the money owed to you that you do not expect to "
        "collect. It sits against Accounts receivable and lowers it, so you "
        "do not overstate what you are owed."
    ),
    "1240": (
        "Money paid to a vendor before their bill arrived. It empties when "
        "the bill is recorded and the advance is set against it."
    ),
    "1260": (
        "What each branch owes to, or is owed by, the other branches. "
        "Across the whole organisation it always adds up to zero."
    ),
    "1300": (
        "VAT you paid on purchases that you can claim back against the VAT "
        "you collect. It grows with each vendor bill that carries VAT."
    ),
    "1400": (
        "The value of stock you hold. It goes up when goods are received "
        "and down when stock is issued or written off."
    ),
    "1500": (
        "The cost of long-lasting items you own, such as buildings, "
        "vehicles, furniture and equipment. It goes up when you buy one and "
        "down when you dispose of one."
    ),
    "1900": (
        "The total wear and tear written off your long-lasting items so "
        "far. It sits against Property, Plant & Equipment and grows each "
        "time depreciation is run."
    ),
    "2000": (
        "The heading for everything the organisation owes. You cannot post "
        "to it; its figure is the total of the accounts under it."
    ),
    "2100": (
        "What you owe vendors for bills you have received but not yet paid. "
        "It goes up when a vendor bill is recorded and down when you pay "
        "it."
    ),
    "2140": (
        "Money a customer has paid ahead of an invoice, such as an "
        "overpayment. It is used up when it is set against a later invoice, "
        "or refunded."
    ),
    "2150": (
        "Goods that have arrived but whose supplier bill has not been "
        "entered yet. It empties when the bill is recorded."
    ),
    "2160": (
        "Fees billed before the service period they cover begins. Each "
        "month of the period moves its share from here into income."
    ),
    "2170": (
        "Refundable deposits you are holding for customers. This is their "
        "money, not your income; it leaves when the deposit is returned, "
        "set against a bill, or forfeited."
    ),
    "2190": (
        "Money one branch received that belongs to another branch. It "
        "empties when the money is passed on to the branch it belongs to."
    ),
    "2195": (
        "Payments that reached a branch's payment terminal before anyone "
        "knew which customer paid. Each one leaves when you match it to the "
        "customer."
    ),
    "2200": (
        "VAT you have collected on sales and owe to the tax authority. It "
        "grows with each invoice that carries VAT and empties when you file "
        "and pay."
    ),
    "2300": (
        "WHT (withholding tax) held back from vendor payments on the tax "
        "authority's behalf. You owe it until you remit it."
    ),
    "2310": (
        "PAYE (income tax) taken from staff pay that you owe the tax "
        "authority. It builds up with each payroll run and empties when you "
        "remit it."
    ),
    "2320": (
        "Pension contributions, from staff pay and from the employer, that "
        "you owe the pension fund administrators. It empties when you pay "
        "them."
    ),
    "2330": (
        "Take-home pay you owe staff once a payroll run is approved. It "
        "empties when the salaries are paid."
    ),
    "2340": (
        "NHF (National Housing Fund) contributions taken from staff pay. "
        "You owe them until you remit them."
    ),
    "2350": (
        "NSITF (employee compensation) contributions you owe the Nigeria "
        "Social Insurance Trust Fund. It builds up with each payroll run "
        "and empties when you pay."
    ),
    "2360": (
        "The ITF (Industrial Training Fund) levy you owe. It builds up with "
        "payroll and empties when you pay it."
    ),
    "2400": (
        "What you owe staff for approved expense claims they paid out of "
        "their own pocket. It empties when you reimburse them."
    ),
    "3000": (
        "The heading for the owners' stake in the organisation: money put "
        "in and profit kept. You cannot post to it."
    ),
    "3100": (
        "Money the owners put into the organisation. It changes only when "
        "capital is put in or taken out."
    ),
    "3200": (
        "Profits kept in the organisation over the years. At each year-end "
        "the year's profit or loss is moved here for you."
    ),
    "4000": (
        "The heading for everything you earn. You cannot post to it; its "
        "figure is the total of the accounts under it."
    ),
    "4100": (
        "Your main earnings from what you charge customers. Each invoice "
        "you issue adds to it, unless the charge is for a later service "
        "period."
    ),
    "4810": (
        "Money received later on a bill you had already written off as "
        "lost."
    ),
    "4820": (
        "Deposits customers did not claim before the deadline, which become "
        "your income once they are forfeited."
    ),
    "4900": (
        "Income you give back when you issue a credit note. It lowers your "
        "total income instead of being shown as an expense."
    ),
    "4910": (
        "Fee reductions and concessions you grant customers. It lowers your "
        "total income and is shown separately so you can see how much you "
        "gave away."
    ),
    "5000": (
        "The heading for everything you spend. You cannot post to it; its "
        "figure is the total of the accounts under it."
    ),
    "5100": (
        "The direct cost of what you sell, such as stock used up. It goes "
        "up when stock is issued or a cost is charged to a sale."
    ),
    "5150": (
        "Stock lost, damaged or found when a count differs from the "
        "records. A write-down is a cost here; a gain lowers it."
    ),
    "5160": (
        "The difference between the price on a vendor's bill and the price "
        "expected when the goods arrived. It is a cost when the bill is "
        "higher."
    ),
    "5200": (
        "What you spend on staff pay. Each approved payroll run charges the "
        "gross pay here."
    ),
    "5210": (
        "The employer's own pension contribution, charged with each payroll "
        "run on top of staff pay."
    ),
    "5220": (
        "The employer's NSITF contribution, charged with each payroll run."
    ),
    "5230": (
        "The ITF (Industrial Training Fund) levy, charged as a cost with "
        "each payroll run."
    ),
    "5300": (
        "Everyday running costs that have no account of their own, such as "
        "office supplies and small repairs. It is where general purchases "
        "go by default."
    ),
    "5350": (
        "Money owed to you that you have given up on collecting, and the "
        "amounts set aside for debts you doubt. Write-offs and provisions "
        "charge it."
    ),
    "5400": (
        "The wear and tear on your long-lasting items, charged to each "
        "period. Every depreciation run adds to it."
    ),
    "5500": (
        "Fees your bank charges you, such as transfer fees and account "
        "upkeep. They are recorded from your bank transactions."
    ),
    "5520": (
        "Online payments that the payer's bank took back. It records the "
        "loss when a chargeback is accepted."
    ),
    "5530": (
        "Differences found when petty cash is counted. A shortage is a cost "
        "here; an overage lowers it."
    ),
}


def fill_blank_purposes(apps, schema_editor):
    Account = apps.get_model("vs_finance", "Account")
    for code, purpose in PURPOSES.items():
        Account.objects.filter(code=code, description="").update(description=purpose)


def keep_on_reverse(apps, schema_editor):
    """Leave the text: a description may since have been edited by a person."""
    return None


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0076_expense_claim_claimant_from_staff_list"),
    ]

    operations = [
        migrations.RunPython(fill_blank_purposes, keep_on_reverse),
    ]
