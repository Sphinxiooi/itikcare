"""Gunicorn config for the ItikCare deployments. Run with:

    gunicorn -c deploy/gunicorn_conf.py itikcare.wsgi:application

from the project root, inside the venv. On a VM, see the other files in deploy/
(itikcare.service, nginx.conf.example) for the rest of the setup; on Railway, see
deploy/RAILWAY.md and railway.toml.
"""

import os

# Railway (and most PaaS hosts) inject $PORT and route public traffic to it, so bind on
# all interfaces there. On the VM there's no $PORT: bind to localhost only, since nginx
# (deploy/nginx.conf.example) is what faces the internet and proxies to this.
_port = os.environ.get("PORT")
bind = f"0.0.0.0:{_port}" if _port else "127.0.0.1:8000"

# Workers: (2 x CPU cores) + 1 is Gunicorn's own rule of thumb. This is a small,
# single-farm-scale app, not a high-traffic service, so 3 is a reasonable default for a
# modest VM -- raise it if `nproc` on the box is higher. WEB_CONCURRENCY overrides it
# (Railway: set it to 2, since every worker holds its own copy of pandas/scikit-learn in
# memory).
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))

# accounts/views.py::signup runs a synchronous bootstrap training call inline (must
# finish before the farmer's first daily log can be forecast) -- generous so that
# request never gets killed mid-training on a slower VM.
timeout = 120

accesslog = "-"  # stdout, picked up by systemd/journald (see deploy/itikcare.service)
errorlog = "-"
