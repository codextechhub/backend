"""Finance setup fields follow their switches on list and upsert routes."""

from core.test_utils import TenantAPIClient
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_bank_document_rework import _ReworkFixture


class SetupFieldAccessTests(_ReworkFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for resource in ("currency", "fxrate", "taxcode", "dimension"):
            install_declared_fields(f"finance.{resource}")
        cls.editor = cls.grant(
            cls.user_for(cls.tenant, "setup-fields@field.test"),
            "finance.currency.create", "finance.fxrate.create",
            "finance.fxrate.view", "finance.taxcode.create",
            "finance.dimension.create", tenant=cls.tenant, role_key="setup-field-editor",
        )
        role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key="setup-field-editor")
        for key in ("finance.currency.symbol", "finance.fxrate.rate",
                    "finance.taxcode.rate_bps", "finance.dimension.allowed_values"):
            set_field_access(role, key, read=False, write=False)

    def test_setup_lists_omit_hidden_values(self):
        from vs_finance.models import Currency, Dimension, FxRate, TaxCode

        Currency.objects.get_or_create(code="USD", defaults={"name": "US Dollar", "symbol": "$"})
        FxRate.objects.create(base_id="NGN", quote_id="USD", rate="0.0007", as_of="2026-01-15")
        TaxCode.objects.create(entity=self.books, code="VAT", name="VAT", rate_bps=750)
        Dimension.objects.create(entity=self.books, code="FUND", name="Fund",
                                 allowed_values=["GRANT-A"])
        client = TenantAPIClient(user=self.editor)
        cases = (
            ("currencies/", "USD", "symbol"),
            ("fx-rates/", None, "rate"),
            ("tax-codes/", "VAT", "rate_bps"),
            ("dimensions/", "FUND", "allowed_values"),
        )
        for route, code, field in cases:
            with self.subTest(route=route):
                response = client.get(self.url(route))
                self.assertEqual(response.status_code, 200, response.data)
                rows = response.data["data"]
                row = next(row for row in rows if code is None or row.get("code") == code)
                self.assertNotIn(field, row)

    def test_setup_upserts_refuse_hidden_fields(self):
        client = TenantAPIClient(user=self.editor)
        cases = (
            ("currencies/", {"code": "GBP", "name": "Pound", "symbol": "£"}),
            ("fx-rates/", {"base": "NGN", "quote": "USD", "rate": "0.0007",
                            "as_of": "2026-01-15"}),
            ("tax-codes/", {"code": "VAT", "name": "VAT", "rate_bps": 750}),
            ("dimensions/", {"code": "FUND", "name": "Fund",
                             "allowed_values": ["GRANT-A"]}),
        )
        for route, body in cases:
            with self.subTest(route=route):
                response = client.post(self.url(route), body, format="json")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_write_denied")
