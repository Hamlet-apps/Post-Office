import pytest
from django.core.cache import cache

from . import helpers


@pytest.fixture(autouse=True)
def _clean_cache(settings):
    settings.PUBLIC_URL = "https://post.maplegrove.example"
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def org(db):
    return helpers.make_org()


@pytest.fixture
def smtp():
    with helpers.FakeSMTP() as fake:
        yield fake
