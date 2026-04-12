# Database Reset Command Documentation

## Overview

`reset_db.py` is a unified Django management command that combines three critical database operations into one safe, sequential workflow:

1. **Delete migration files** (except `__init__.py`)
2. **Drop all database tables**
3. **Run fresh migrations** (`makemigrations` + `migrate`)

This command replaces the need to run three separate commands manually and ensures they execute in the correct order.

---

## Installation

1. **Create the management command directory structure** (if it doesn't exist):
   ```bash
   mkdir -p <your_project>/management/commands
   touch <your_project>/management/__init__.py
   touch <your_project>/management/commands/__init__.py
   ```

2. **Place the command file**:
   ```
   <your_project>/
   ├── management/
   │   ├── __init__.py
   │   └── commands/
   │       ├── __init__.py
   │       └── reset_db.py    ← Place the file here
   ```

3. **Verify installation**:
   ```bash
   python manage.py help reset_db
   ```

---

## Usage

### Basic Usage (Interactive Mode)

Run with confirmation prompts at each step:

```bash
python manage.py reset_db
```

**What happens:**
- Displays warning banner
- Asks for global confirmation
- Asks for confirmation before each step:
  - Step 1: Delete migration files
  - Step 2: Drop database tables
  - Step 3: Run fresh migrations

### Auto-Confirm Mode (Use with Caution)

Skip all confirmation prompts:

```bash
python manage.py reset_db --yes
```

⚠️ **Warning:** This will execute all operations without asking. Use only in development environments or automated scripts.

### Specify Database

Use a different database alias (from `settings.DATABASES`):

```bash
python manage.py reset_db --database secondary_db
```

### Skip Individual Steps

Skip specific operations using flags:

```bash
# Skip deleting migration files (only drop tables and migrate)
python manage.py reset_db --skip-migrations

# Skip dropping tables (only delete migrations and run fresh migrations)
python manage.py reset_db --skip-drop

# Skip running migrations (only delete migrations and drop tables)
python manage.py reset_db --skip-migrate
```

### Combine Options

```bash
# Drop tables and migrate, but keep migration files
python manage.py reset_db --skip-migrations --yes

# Delete migrations and drop tables, but don't run fresh migrations
python manage.py reset_db --skip-migrate

# Use a different database with auto-confirm
python manage.py reset_db --database secondary_db --yes
```

### Run with Post-Migration Commands

Execute seeding or setup commands automatically after migrations:

```bash
# Reset and seed in one go
python manage.py reset_db --post-commands seed_roles seed_schools

# With auto-confirm
python manage.py reset_db --yes --post-commands seed_roles loaddata fixtures/initial.json

# Complex workflow
python manage.py reset_db --skip-migrations --post-commands seed_defaults create_superuser
```

---

## Command-Line Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--database` | String | `'default'` | Database alias to use (from `settings.DATABASES`) |
| `--skip-migrations` | Flag | `False` | Skip deleting migration files |
| `--skip-drop` | Flag | `False` | Skip dropping database tables |
| `--skip-migrate` | Flag | `False` | Skip running `makemigrations` and `migrate` |
| `--yes` | Flag | `False` | Auto-confirm all prompts (bypass interactive confirmation) |
| `--post-commands` | List | `None` | Commands to run after migrations (e.g., `seed_roles seed_schools`) |

---

## What Each Step Does

### Step 1: Delete Migration Files

**Purpose:**  
Removes all migration files from specified Django apps (except `__init__.py`).

**Apps processed** (edit in the command file):
- `vs_admin_console`
- `vs_user`
- `vs_schools`
- `vs_rbac`
- `vs_audit`

**How to customize:**  
Edit the `installed_apps` list in the `_delete_migration_files` method:

```python
installed_apps = [
    'vs_admin_console',
    'vs_user',
    'vs_schools',
    'vs_rbac',
    'vs_audit',
    'your_new_app',  # Add your apps here
]
```

**Output example:**
```
STEP 1: Deleting migration files
--------------------------------------------------
Delete migrations from: vs_admin_console, vs_user, vs_schools, vs_rbac, vs_audit? [y/N]: y
  ✓ Deleted 0001_initial.py
  ✓ Deleted 0002_auto_20240101_1200.py
  ✓ Deleted 0003_add_fields.py

Deleted 15 migration file(s)
```

---

### Step 2: Drop Database Tables

**Purpose:**  
Drops all tables in the database. Handles foreign key constraints properly for different database vendors.

**Supported databases:**
- ✅ **PostgreSQL** (uses `DROP TABLE ... CASCADE`)
- ✅ **MySQL** (disables foreign key checks)
- ✅ **SQLite** (simple drop)

**How it works:**
1. Detects database vendor automatically
2. Retrieves list of all tables using Django's introspection
3. Drops each table using vendor-specific SQL
4. Handles foreign key constraints appropriately

**Output example:**
```
STEP 2: Dropping database tables
--------------------------------------------------
Drop ALL tables in the database? [y/N]: y
Database vendor: postgresql
Found 23 table(s) to drop
  ✓ Dropped auth_user
  ✓ Dropped vs_schools_school
  ✓ Dropped vs_rbac_role
  ...

Successfully dropped 23 table(s)
```

---

### Step 3: Run Fresh Migrations

**Purpose:**  
Creates new migration files and applies them to build a fresh database schema.

**Commands executed:**
1. `python manage.py makemigrations`
2. `python manage.py migrate --database <database_alias>`

**Output example:**
```
STEP 3: Running fresh migrations
--------------------------------------------------
Run makemigrations and migrate? [y/N]: y

Running makemigrations...
Migrations for 'vs_user':
  vs_user/migrations/0001_initial.py
    - Create model User
    - Create model Profile
    ...

Running migrate...
Operations to perform:
  Apply all migrations: admin, auth, contenttypes, sessions, vs_user, vs_schools, vs_rbac
Running migrations:
  Applying contenttypes.0001_initial... OK
  Applying auth.0001_initial... OK
  ...

Fresh migrations completed successfully
```

---

### Step 4: Run Post-Migration Commands (Optional)

**Purpose:**  
Automatically run custom Django commands after migrations complete. Useful for seeding, loading fixtures, or initializing data.

**How to use:**
```bash
python manage.py reset_db --post-commands seed_roles seed_schools create_superuser
```

**Supported command formats:**
- Simple command: `seed_roles`
- With arguments: `"loaddata initial_data.json"`
- Multiple commands: `seed_roles seed_schools setup_defaults`

**How it works:**
1. Runs after migrations complete successfully
2. Executes commands in the order provided
3. Asks for confirmation (unless `--yes` flag is used)
4. If a command fails, asks if you want to continue with remaining commands

**Output example:**


---

## Safety Features

### 1. Warning Banner
Displays a clear warning about the destructive nature of the operation.

### 2. Multi-Level Confirmation
- Global confirmation before any operation
- Individual confirmation for each step
- Can be bypassed with `--yes` flag

### 3. Preserves Critical Files
- Never deletes `__init__.py` files in migrations directories
- Only targets migration `.py` files

### 4. Database Vendor Detection
- Automatically detects database type (PostgreSQL, MySQL, SQLite)
- Uses appropriate SQL syntax for each vendor
- Handles foreign key constraints correctly

### 5. Error Handling
- Try-catch blocks around each operation
- Graceful error messages
- Continues with remaining operations if one fails (where safe)

### 6. Keyboard Interrupt Handling
- Safely handles Ctrl+C during prompts
- Cancels operation without partial execution

---

## Common Use Cases

### Use Case 1: Complete Database Reset (Development)

**Scenario:**  
You've made major model changes and want to start fresh.

**Command:**
```bash
python manage.py reset_db
```

**Result:**
- All migrations deleted
- All tables dropped
- Fresh schema created

---

### Use Case 2: Fix Migration Conflicts

**Scenario:**  
You have migration conflicts and want to rebuild from scratch.

**Command:**
```bash
python manage.py reset_db
```

**Manual steps after:**
If you have initial data, run:
```bash
python manage.py loaddata initial_data.json
```

---

### Use Case 3: Testing Fresh Migrations

**Scenario:**  
You want to test that migrations run cleanly from scratch.

**Command:**
```bash
python manage.py reset_db --yes
```

Use `--yes` to avoid manual confirmation in automated test scripts.

---

### Use Case 4: Keep Migration Files, Just Reset Database

**Scenario:**  
Your migrations are fine, but you want a clean database.

**Command:**
```bash
python manage.py reset_db --skip-migrations
```

**Result:**
- Migration files preserved
- Tables dropped
- Existing migrations re-applied

---

### Use Case 5: Production-Like Test (Secondary Database)

**Scenario:**  
Testing on a secondary database before touching production.

**Command:**
```bash
python manage.py reset_db --database test_db
```

**Result:**
- Operations run on `test_db` instead of `default`
- Primary database untouched

---

### Use Case 6: Complete Reset with Seeding

**Scenario:**  
You want to reset everything and automatically seed default data.

**Command:**
```bash
python manage.py reset_db --yes --post-commands seed_roles seed_schools seed_fee_templates
```

**Result:**
- Migrations deleted
- Tables dropped
- Fresh schema created
- Default roles seeded
- School templates seeded
- Fee templates seeded

**Alternative with fixture loading:**
```bash
python manage.py reset_db --post-commands "loaddata initial_roles.json" "loaddata initial_schools.json"
```

---

## Troubleshooting

### Issue: "No module named 'management'"

**Cause:**  
Missing `__init__.py` files in the management directory structure.

**Solution:**
```bash
touch <your_project>/management/__init__.py
touch <your_project>/management/commands/__init__.py
```

---

### Issue: "Database vendor not supported"

**Cause:**  
Using a database type that isn't PostgreSQL, MySQL, or SQLite.

**Solution:**  
Edit the `_drop_database_tables` method to add support for your database:

```python
elif vendor == 'oracle':
    # Add Oracle-specific logic here
    pass
```

---

### Issue: Tables not dropping due to permissions

**Cause:**  
Database user lacks `DROP` privileges.

**Solution:**
```sql
-- PostgreSQL
GRANT ALL PRIVILEGES ON DATABASE your_db TO your_user;

-- MySQL
GRANT DROP ON your_db.* TO 'your_user'@'localhost';
```

---

### Issue: Migration files not being detected

**Cause:**  
App names in the command don't match your actual app names.

**Solution:**  
Edit the `installed_apps` list in `_delete_migration_files` method to match your apps.

---

### Issue: "django.db.utils.ProgrammingError: relation does not exist"

**Cause:**  
Trying to migrate when tables from a previous schema still exist.

**Solution:**  
Run the full command without skipping any steps:
```bash
python manage.py reset_db --yes
```

---

## Differences from Original Commands

### vs. `del_migration.py`
- ✅ Added command-line flags to skip this step
- ✅ Better error handling
- ✅ Part of a unified workflow

### vs. `drop_table.py`
- ✅ **Database-agnostic** (works with PostgreSQL, MySQL, SQLite)
- ✅ No longer requires `pymysql` or hardcoded credentials
- ✅ Uses Django's database connection API
- ✅ Automatically detects database vendor
- ✅ Removed interactive table selection (drops all tables)
- ✅ Part of a unified workflow

### vs. `migrate_all.py`
- ✅ Works with any database alias
- ✅ Can be skipped via flag
- ✅ Part of a unified workflow

---

## Advanced Customization

### 1. Make App List Dynamic

Instead of hardcoding apps, use `settings.INSTALLED_APPS`:

```python
def _delete_migration_files(self):
    # Get only local apps (exclude third-party)
    from django.apps import apps
    installed_apps = [
        app.name for app in apps.get_app_configs()
        if app.name.startswith('vs_')  # Customize prefix
    ]
    # ... rest of the method
```

### 2. Add Pre/Post Hooks

Add custom logic before or after each step:

```python
def _delete_migration_files(self):
    # Pre-hook: Backup migrations
    self._backup_migrations()
    
    # ... existing logic ...
    
    # Post-hook: Log to external service
    self._log_to_monitoring("migrations_deleted")
```

### 3. Add Dry Run Mode

Add a `--dry-run` flag to see what would happen:

```python
def add_arguments(self, parser):
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )
```

---

## Best Practices

### ✅ DO:
- Use in **development environments** frequently
- Run with `--yes` in automated CI/CD pipelines
- Test migrations from scratch regularly
- Back up production data before similar operations

### ❌ DON'T:
- **Never** run in production without backups
- **Never** run on production database directly
- **Don't** skip confirmation prompts on shared databases
- **Don't** run during active development by other team members

---

## Examples

### Example 1: Fresh Start (Development)
```bash
# Delete everything and start fresh
python manage.py reset_db --yes
```

### Example 2: Keep Migrations, Reset Database
```bash
# Useful when migrations are correct but data is messy
python manage.py reset_db --skip-migrations
```

### Example 3: Testing Migration Flow
```bash
# Delete migrations, drop tables, create fresh migrations
python manage.py reset_db

# Then load fixtures
python manage.py loaddata initial_roles.json
python manage.py loaddata test_schools.json
```

### Example 4: CI/CD Pipeline
```bash
#!/bin/bash
# reset_test_db.sh

# Reset test database
python manage.py reset_db --database test_db --yes

# Load test fixtures
python manage.py loaddata test_fixtures.json --database test_db

# Run tests
python manage.py test --settings=config.settings.test
```

---

## Migration to This Command

If you were previously using the three separate commands:

### Old Workflow:
```bash
python manage.py del_migration
python manage.py drop_table cx_db
python manage.py migrate_all
```

### New Workflow:
```bash
python manage.py reset_db --yes
```

### Benefits:
- ✅ One command instead of three
- ✅ Correct execution order guaranteed
- ✅ Database-agnostic (no MySQL hardcoding)
- ✅ Better error handling
- ✅ More flexible options
- ✅ Works with PostgreSQL on Render

---

## Summary

The `reset_db` command provides a safe, comprehensive way to reset your database and migrations in the correct order. It combines the functionality of three separate commands while adding:

- Database vendor detection
- Better error handling
- Flexible skip options
- Safety confirmations
- Clear progress output

Use it confidently in development to maintain clean migration histories and test fresh schema builds.