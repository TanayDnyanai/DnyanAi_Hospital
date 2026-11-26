"""Convenience entry point: python run.py"""

from __future__ import annotations

import uvicorn

from backend import config

if __name__ == "__main__":
    uvicorn.run(
        "backend.app:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        reload=True,
    )
