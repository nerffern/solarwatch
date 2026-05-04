"""Local development entrypoint.

Run with:
    python run.py

Uses uvicorn with hot-reload enabled. Do not use this in production.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
