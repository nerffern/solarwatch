"""Production entrypoint.

Used by uvicorn or gunicorn in K8s / Docker:

    uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
    gunicorn main:app -w 2 -k uvicorn.workers.UvicornWorker

Multiple workers per pod is fine — each worker gets its own DB connection pool
and the session state lives in signed cookies, not server memory.
"""

from app import create_app

app = create_app()
