"""RSS 失败不得被缓存成成功或解释为没有反证；全部使用隔离目录。"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from sqmy.collector import SourceCollector
from sqmy.config import Settings


@pytest.fixture
def collector(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/sources.toml").write_text('sources=[]')
    raw = deepcopy(Settings.load(Path(__file__).parents[1] / "config/settings.toml").raw)
    return SourceCollector(tmp_path, raw)


@contextmanager
def response(body):
    class Response:
        def read(self, size):
            return body.encode()[:size]
    yield Response()


@pytest.mark.parametrize("body", [
    '<!doctype html><html><head><title>搜索 - Microsoft 必应</title></head><body></body></html>',
    '<!doctype html><html><meta charset="utf-8"><body>搜索首页</body></html>',
    '<rss><channel>',
    '<rss/>',
])
@pytest.mark.parametrize("namespace", ["feeds", "counterevidence"])
def test_invalid_network_response_not_cached(collector, body, namespace):
    with patch("sqmy.collector.urlopen", return_value=response(body)) as fetch:
        with pytest.raises(ValueError, match="RSS"):
            collector._fetch_query("测试", namespace)
    assert fetch.call_count == 1
    assert not list(collector.root.glob("data/cache/**/*.xml"))


def test_cached_html_reports_error_without_refetch_or_deletion(collector):
    url = "https://example.test/rss"
    cache = collector.root / "data/cache/counterevidence" / (hashlib.sha256(url.encode()).hexdigest() + ".xml")
    cache.parent.mkdir(parents=True)
    body = '<html><body>搜索首页</body></html>'
    cache.write_text(body)
    with patch("sqmy.collector.urlopen", side_effect=AssertionError("不得重复请求")):
        with pytest.raises(ValueError, match="RSS"):
            collector._fetch_url(url, "counterevidence")
    assert cache.read_text() == body


def test_valid_empty_rss_is_success_and_reuses_cache(collector):
    body = '<rss><channel><title>查询结果</title></channel></rss>'
    with patch("sqmy.collector.urlopen", return_value=response(body)) as fetch:
        assert collector.search_query("无匹配政策") == []
        assert collector.search_query("无匹配政策") == []
    assert fetch.call_count == 1


@pytest.mark.parametrize("body", ['<html><body>首页</body></html>', '<rss><channel>', ''])
def test_counterevidence_parse_error_propagates(collector, body):
    # 即使来自旧缓存或替代抓取器，也不得返回成功的空列表。
    with patch.object(collector, "_fetch_query", return_value=body):
        with pytest.raises(ValueError, match="RSS"):
            collector.search_query("现行政策")


def test_discovery_records_invalid_rss_as_error(collector):
    source = dict(id="broken", name="损坏来源", level=1, region="北京", type="rss_search")
    stats = []
    assert collector._items_from_payloads([
        {"source": source, "xml": '<html><body>首页</body></html>'},
    ], _stats=stats) == []
    assert "RSS" in stats[0]["parse_error"]
    assert stats[0]["collected_count"] == 0


def test_direct_html_index_still_caches_html(collector):
    body = '<html><body>官方文章目录</body></html>'
    with patch("sqmy.collector.urlopen", return_value=response(body)) as fetch:
        assert collector._fetch_url("https://example.gov.cn/list", "indexes") == body
        assert collector._fetch_url("https://example.gov.cn/list", "indexes") == body
    assert fetch.call_count == 1
