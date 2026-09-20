import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def no_workspace_database_in_tests(monkeypatch):
    """回归只能用隔离库，连只读测试也不得隐式初始化用户真实历史。"""
    from sqmy.db import Database
    original = Database.connect
    real_path = (Path(__file__).parents[1] / 'data/history/workflow.db').resolve()
    def isolated(self):
        if self.path.resolve() == real_path:
            raise AssertionError('普通测试不得连接工作区真实数据库')
        return original(self)
    monkeypatch.setattr(Database, 'connect', isolated)


@pytest.fixture(autouse=True)
def no_real_tavily_in_tests(monkeypatch):
    """即使用户本机已配置密钥，普通回归也不能发起新接入的付费请求。"""
    def blocked(*args, **kwargs):
        raise AssertionError("Tavily网络请求必须由测试显式stub")
    monkeypatch.setattr("sqmy.tavily.TavilyClient._post", blocked)
