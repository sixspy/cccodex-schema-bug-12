import pytest

from app.main import create_app


@pytest.fixture
async def client(aiohttp_client, tmp_path):
    db = tmp_path / "test.db"
    app = create_app(str(db))
    return await aiohttp_client(app)


@pytest.fixture
async def authed(client):
    r = await client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status == 200
    return client
