"""Single place that loads local configuration for the chatbot.

Paths are tried in increasing priority order, with ``override=False`` so
injected deployment secrets always win:

1. workspace above the repo (the shared local ``.env.local``)
2. repo root
3. the chatbot directory itself
"""

from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
OUTER_WORKSPACE = REPO_ROOT.parent


def load_app_env() -> None:
    for path in (
        OUTER_WORKSPACE / ".env.local",
        OUTER_WORKSPACE / ".env",
        REPO_ROOT / ".env.local",
        REPO_ROOT / ".env",
        APP_DIR / ".env",
    ):
        if path.is_file():
            load_dotenv(path, override=False)
