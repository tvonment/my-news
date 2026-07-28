from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The Functions app root is the import root at runtime; mirror that for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TEST_ENV = {
    "COSMOS_ENDPOINT": "https://example.documents.azure.com:443/",
    "COSMOS_DATABASE": "mynews",
    "COSMOS_CONTAINER": "items",
    "FOUNDRY_ENDPOINT": "https://example.openai.azure.com",
    "MODEL_SCORER": "scorer",
    "MODEL_SUMMARIZER": "summarizer",
    "MODEL_EMBEDDING": "embedding",
}


@pytest.fixture(autouse=True)
def _env() -> None:
    for key, value in _TEST_ENV.items():
        os.environ.setdefault(key, value)
    from core.config import get_settings

    get_settings.cache_clear()
