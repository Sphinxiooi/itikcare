"""Gunicorn config for the ItikCare VM deployment. Run with:

    gunicorn -c deploy/gunicorn_conf.py itikcare.wsgi:application

from the project root, inside the venv. See DEPLOYMENT.md for the full setup.
"""

bind = "127.0.0.1:8000"  # nginx (deploy/nginx.conf.example) proxies to this

# Workers: (2 x CPU cores) + 1 is Gunicorn's own rule of thumb. This is a small,
# single-farm-scale app, not a high-traffic service, so 3 is a reasonable default for a
# modest VM -- raise it if `nproc` on the box is higher.
workers = 3

# accounts/views.py::signup runs a synchronous bootstrap training call inline (must
# finish before the farmer's first daily log can be forecast) -- generous so that
# request never gets killed mid-training on a slower VM.
timeout = 60

accesslog = "-"  # stdout, picked up by systemd/journald (see deploy/itikcare.service)
errorlog = "-"
