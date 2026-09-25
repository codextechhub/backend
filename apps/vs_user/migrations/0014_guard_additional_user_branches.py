"""Keep equal account postings inside the tenant and off platform accounts."""

from django.db import migrations


FORWARD = """
CREATE OR REPLACE FUNCTION vs_user_platform_no_branch() RETURNS trigger AS $$
DECLARE
    tenant_kind text;
BEGIN
    SELECT kind INTO tenant_kind FROM vs_tenants_tenant WHERE id = NEW.tenant_id;
    IF tenant_kind = 'PLATFORM' AND (
        NEW.branch_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM vs_users_user_additional_branches
            WHERE user_id = NEW.id
        )
    ) THEN
        RAISE EXCEPTION 'ck_platform_user_no_branch: a platform user cannot hold a branch.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM vs_users_user_additional_branches ab
        JOIN vs_schools_branch b ON b.id = ab.branch_id
        WHERE ab.user_id = NEW.id AND b.tenant_id <> NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'ck_user_additional_branch_tenant: postings must belong to the account tenant.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION vs_tenants_platform_kind_no_branch_users()
RETURNS trigger AS $$
BEGIN
    IF NEW.kind = 'PLATFORM' AND (
        EXISTS (SELECT 1 FROM vs_users_user
                WHERE tenant_id = NEW.id AND branch_id IS NOT NULL)
        OR EXISTS (
            SELECT 1 FROM vs_users_user_additional_branches ab
            JOIN vs_users_user u ON u.id = ab.user_id
            WHERE u.tenant_id = NEW.id
        )
    ) THEN
        RAISE EXCEPTION 'ck_platform_user_no_branch: tenant cannot become PLATFORM while its users hold branches.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION vs_user_additional_branch_guard()
RETURNS trigger AS $$
DECLARE
    account_tenant bigint;
    tenant_kind text;
    branch_tenant bigint;
BEGIN
    SELECT u.tenant_id, t.kind INTO account_tenant, tenant_kind
      FROM vs_users_user u
      JOIN vs_tenants_tenant t ON t.id = u.tenant_id
     WHERE u.id = NEW.user_id
     FOR SHARE OF u, t;
    SELECT tenant_id INTO branch_tenant
      FROM vs_schools_branch WHERE id = NEW.branch_id;
    IF tenant_kind = 'PLATFORM' THEN
        RAISE EXCEPTION 'ck_platform_user_no_branch: a platform user cannot hold a branch.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF account_tenant <> branch_tenant THEN
        RAISE EXCEPTION 'ck_user_additional_branch_tenant: postings must belong to the account tenant.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER vs_user_additional_branch_guard
    BEFORE INSERT OR UPDATE OF user_id, branch_id
    ON vs_users_user_additional_branches
    FOR EACH ROW EXECUTE FUNCTION vs_user_additional_branch_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS vs_user_additional_branch_guard
    ON vs_users_user_additional_branches;
DROP FUNCTION IF EXISTS vs_user_additional_branch_guard();

CREATE OR REPLACE FUNCTION vs_user_platform_no_branch() RETURNS trigger AS $$
DECLARE
    tenant_kind text;
BEGIN
    IF NEW.branch_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT kind INTO tenant_kind FROM vs_tenants_tenant WHERE id = NEW.tenant_id;
    IF tenant_kind = 'PLATFORM' THEN
        RAISE EXCEPTION 'ck_platform_user_no_branch: a user of a PLATFORM tenant must not be assigned to a branch.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION vs_tenants_platform_kind_no_branch_users()
RETURNS trigger AS $$
BEGIN
    IF NEW.kind = 'PLATFORM' AND EXISTS (
        SELECT 1 FROM vs_users_user
        WHERE tenant_id = NEW.id AND branch_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'ck_platform_user_no_branch: tenant cannot become PLATFORM while its users hold branches.'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD)


def remove(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE)


class Migration(migrations.Migration):
    dependencies = [("vs_user", "0013_user_additional_branches")]

    operations = [migrations.RunPython(install, remove)]
