from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app


def test_demo_lifecycle_and_security(tmp_path):
    db = Database("sqlite:///" + (tmp_path / "demo.sqlite3").as_posix())
    app = create_app(Settings(_env_file=None), db)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        response = client.get("/")
        assert response.status_code == 200
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        viewer = client.get("/api/demo/profile").json()
        assert viewer["has_photo"] is False and "photo_file_id" not in viewer
        feed = client.get("/api/demo/feed").json()
        assert len(feed) == 7
        assert all("telegram_id" not in row["profile"] and "steam_id" not in row["profile"] for row in feed)
        target = next(row["profile"]["id"] for row in feed if row["profile"]["name"] == "Мира")
        result = client.post("/api/demo/reaction",json={"target_id":target,"kind":"like"}).json()
        assert result["matched"]
        assert len(client.get("/api/demo/matches").json()) == 1
        viewer["age"] = 17
        assert client.put("/api/demo/profile",json=viewer).status_code == 422
        assert client.post("/api/demo/reset").status_code == 200
        assert client.get("/api/demo/matches").json() == []
        assert client.get("/steam/login?ticket=bad").status_code == 404
        assert client.get("/model/report.json").json()["training_users"] > 10000


def test_demo_endpoints_disabled_with_live_token(tmp_path):
    settings = Settings(_env_file=None, bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", public_url="https://party.example", steam_mode="openid")
    app = create_app(settings, Database("sqlite:///" + (tmp_path / "live.sqlite3").as_posix()), start_bot=False)
    with TestClient(app) as client:
        assert client.get("/api/demo/profile").status_code == 404
        assert client.get("/api/demo/feed").status_code == 404
        assert client.get("/docs").status_code == 404
        miniapp = client.get("/miniapp")
        assert miniapp.status_code == 200 and "PartyUp" in miniapp.text
        assert "https://web.telegram.org" in miniapp.headers["content-security-policy"]
        assert "x-frame-options" not in miniapp.headers
        assert client.get("/").headers["x-frame-options"] == "DENY"
        cards = client.get("/miniapp/deck").json()
        assert cards and all(card["games"] and "telegram_id" not in card for card in cards)
        for card in cards:
            assert client.get(card["photo"]).headers["content-type"].startswith("image/")
        assert client.get("/miniapp/portrait/unknown").status_code == 404
        assert "Telegram" in client.get("/").text
        assert client.get("/steam/login?ticket=forged").status_code == 400


def test_forced_demo_never_starts_live_bot(tmp_path):
    settings = Settings(_env_file=None, bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                        force_demo=True)
    app = create_app(settings, Database("sqlite:///" + (tmp_path / "forced.sqlite3").as_posix()))
    with TestClient(app) as client:
        assert client.get("/health").json()["mode"] == "demo"
        assert app.state.bot is None
        assert client.get("/api/demo/profile").json()["name"] == "Ты"
