"""The SQL behind the kept-document triggers, shared by two apps' migrations.

``vs_finance`` 0078 creates the functions and puts the triggers on finance
tables; ``vs_procurement`` 0049 puts the same triggers on procurement tables.
The leading underscore keeps Django's migration loader from reading this as a
migration. Frozen once applied: change the SQL in a new migration, not here.
"""

FUNCTIONS = """
CREATE OR REPLACE FUNCTION xvs_kept_document_no_delete() RETURNS trigger AS $$
BEGIN
    IF OLD.status <> 'DRAFT' THEN
        RAISE EXCEPTION '% row % is %, a kept record, and cannot be deleted.',
            TG_TABLE_NAME, OLD.id, OLD.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION xvs_kept_document_no_unpost() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('POSTED', 'REVERSED')
            AND NEW.status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED') THEN
        RAISE EXCEPTION '% row % is % and cannot go back to %.',
            TG_TABLE_NAME, OLD.id, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- TG_ARGV: the document table, then the line's column naming the document.
CREATE OR REPLACE FUNCTION xvs_kept_line_no_delete() RETURNS trigger AS $$
DECLARE
    parent_status text;
BEGIN
    EXECUTE format('SELECT status FROM %I WHERE id = $1', TG_ARGV[0])
        INTO parent_status
        USING (to_jsonb(OLD) ->> TG_ARGV[1])::bigint;
    IF parent_status IS NOT NULL AND parent_status <> 'DRAFT' THEN
        RAISE EXCEPTION '% row % belongs to a % document in %, and cannot be deleted.',
            TG_TABLE_NAME, OLD.id, parent_status, TG_ARGV[0]
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""


def document_triggers(table):
    return f"""
DROP TRIGGER IF EXISTS {table}_kept_no_delete ON {table};
CREATE TRIGGER {table}_kept_no_delete
    BEFORE DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION xvs_kept_document_no_delete();
DROP TRIGGER IF EXISTS {table}_kept_no_unpost ON {table};
CREATE TRIGGER {table}_kept_no_unpost
    BEFORE UPDATE OF status ON {table}
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION xvs_kept_document_no_unpost();
"""


def drop_document_triggers(table):
    return f"""
DROP TRIGGER IF EXISTS {table}_kept_no_delete ON {table};
DROP TRIGGER IF EXISTS {table}_kept_no_unpost ON {table};
"""


def line_triggers(table, parent, column):
    return f"""
DROP TRIGGER IF EXISTS {table}_kept_no_delete ON {table};
CREATE TRIGGER {table}_kept_no_delete
    BEFORE DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION xvs_kept_line_no_delete('{parent}', '{column}');
"""
