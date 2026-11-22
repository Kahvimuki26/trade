import pytest

from tradebot.data import Synthetic


@pytest.fixture
def market():
    return Synthetic(hours=24 * 120, seed=7)
