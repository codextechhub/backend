# MySQL driver shim — only needed when running against MariaDB/MySQL
# (DB_ENGINE=mysql fallback). PostgreSQL environments work without PyMySQL.
try:
    import pymysql
except ImportError:  # pragma: no cover
    pass
else:
    pymysql.install_as_MySQLdb()

from .celery import app as celery_app  # noqa: F401

__all__ = ("celery_app",)