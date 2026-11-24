"""Gunicorn config. Run: gunicorn -c deploy/gunicorn_conf.py itikcare.wsgi:application"""

import os

# Bind all interfaces when the host sets $PORT, else localhost behind nginx
_port = os.environ.get("PORT")
bind = f"0.0.0.0:{_port}" if _port else "127.0.0.1:8000"

# Worker count (WEB_CONCURRENCY overrides it; each worker holds its own pandas/scikit-learn)
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))

# Long timeout: signup runs bootstrap training inline
timeout = 120

accesslog = "-"  # stdout, picked up by systemd/journald (see deploy/itikcare.service)
errorlog = "-"
