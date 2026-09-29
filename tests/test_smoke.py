"""冒烟测试骨架。"""


def test_skeleton_imports() -> None:
    import app.api  # noqa: F401
    import app.billing  # noqa: F401
    import app.metering  # noqa: F401
    import app.services  # noqa: F401
    import app.settlement  # noqa: F401
    import app.storage  # noqa: F401
    import main  # noqa: F401
