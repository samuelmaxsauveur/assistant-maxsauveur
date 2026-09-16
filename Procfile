# The scheduler runs inside the web process (see app.py). A separate worker
# service would get its own container and its own empty SQLite file, so it
# would never see the rows written here.
web: gunicorn app:app -c gunicorn.conf.py
