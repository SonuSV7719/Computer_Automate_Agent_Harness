"""Run the brain on this machine for development: memory storage, no sign-in.

    python -m brain.local            # http://127.0.0.1:8090
Needs GEMINI_API_KEY for learning, healing and file reading.
"""
import os

import uvicorn

from brain.app.main import create_app
from brain.app.store import MemoryStore

if __name__ == "__main__":
    os.environ.setdefault("SHADOW_SANDBOX_TOKEN", "")
    uvicorn.run(create_app(MemoryStore(), auth_mode="dev"), host="127.0.0.1",
                port=int(os.environ.get("PORT", "8090")), log_level="info")
