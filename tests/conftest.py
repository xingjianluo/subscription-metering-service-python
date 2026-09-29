"""测试日志：打印事件、账期与判定依据。"""

import logging

import pytest


@pytest.fixture(autouse=True)
def _metering_logs(caplog):
    caplog.set_level(logging.INFO, logger="metering")
    yield
