### Python (pytest)
- Name files test_*.py; put shared fixtures in a conftest.py inside the component's test folder.
- Import project modules exactly as the project's own code does (pytest runs from the component folder).
- Use tmp_path for files, monkeypatch for env vars and time, unittest.mock for network calls.
- Use pytest.mark.parametrize for edge cases instead of copy-pasted tests.
