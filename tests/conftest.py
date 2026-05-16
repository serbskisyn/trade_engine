"""Pytest configuration: isolated DB pro Test, Singleton-Reset."""
import pytest
from app.engine import trade_manager as tm


@pytest.fixture
async def isolated_db(tmp_path, monkeypatch):
    """Patcht DB_PATH auf eine temporäre Datei und resettet den Connection-Singleton."""
    db_file = tmp_path / "trades.db"
    monkeypatch.setattr(tm, "DB_PATH", str(db_file))
    await tm.close_db()
    yield db_file
    await tm.close_db()
