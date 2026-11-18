import os
from aiohttp import web

from app.main import create_app

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7100"))
    web.run_app(create_app(), host=host, port=port)
