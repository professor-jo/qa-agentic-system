### Python web (FastAPI / Flask / Django)
- FastAPI: from fastapi.testclient import TestClient; client = TestClient(app). Override dependencies
  (app.dependency_overrides) to use an in-memory SQLite database or fakes.
- Flask: app.test_client(); set app.config["TESTING"] = True.
- Django: use django.test.TestCase / Client; settings must point at a test database.
- Set required environment variables in a conftest.py fixture (monkeypatch.setenv), not in source code.
