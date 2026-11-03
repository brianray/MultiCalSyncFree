"""Local dev entry point: loads .env (if present) then serves on $PORT.

    python -m app.local_server
"""

from __future__ import annotations

import os

import uvicorn


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    load_dotenv()
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), reload=True)
