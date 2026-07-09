# mysqlclient is the primary MySQL driver (confirmed working on this project's dev
# machine). This shim is a documented fallback only: if mysqlclient ever fails to
# install on a given machine, `pip install PyMySQL` makes it a drop-in replacement
# with no other code changes. No-op when PyMySQL isn't installed.
try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:
    pass
