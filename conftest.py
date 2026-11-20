import pytest


@pytest.fixture(autouse=True)
def english_wording(request, settings):
    """Tests assert on the English wording, whatever language a reader has.

    The Ukrainian and French catalogues are switched off unless a test is marked
    with @pytest.mark.real_translations (see core/tests/test_translations.py).
    Changing LOCALE_PATHS makes Django drop its cached translations.
    """
    if request.node.get_closest_marker("real_translations") is None:
        settings.LOCALE_PATHS = []
    yield
