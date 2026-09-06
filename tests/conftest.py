import pytest


@pytest.fixture(autouse=True)
def no_real_tavily_in_tests(monkeypatch):
    """即使用户本机已配置密钥，普通回归也不能发起新接入的付费请求。"""
    def blocked(*args, **kwargs):
        raise AssertionError("Tavily网络请求必须由测试显式stub")
    monkeypatch.setattr("sqmy.tavily.TavilyClient._post", blocked)
