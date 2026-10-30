"""API contract checks.

These do not touch the database: they assert the shape of the API the frontend
is written against, and the defaults that would silently change behaviour if
someone edited a model. Importing the app is safe because the database is only
opened during startup, and no lifespan is run here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402


def _client() -> TestClient:
    # No context manager: do not run lifespan, do not start the worker.
    return TestClient(app)


def test_openapi_builds_and_core_routes_exist():
    schema = _client().get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    for expected in ("/api/series", "/api/health"):
        assert expected in paths, f"{expected} missing from the API"


def test_series_summary_defaults_to_detected_language():
    components = _client().get("/openapi.json").json()["components"]["schemas"]
    summary = components["SeriesSummary"]["properties"]
    assert summary["source_language"]["default"] == "auto"
    assert summary["detected_language"]["default"] == ""
    assert summary["target_language"]["default"] == "English"


def test_manual_series_defaults_to_auto_source_language():
    components = _client().get("/openapi.json").json()["components"]["schemas"]
    manual = components["ManualSeriesIn"]["properties"]
    assert manual["source_language"]["default"] == "auto"
