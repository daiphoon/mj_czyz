from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET

import pytest

from sqmy.collector import SourceCollector, listing_to_rss
from sqmy.config import Settings
from sqmy.maintenance import preflight


def medical_source():
    return dict(id="haidian_medical_direct_index", name="医保公开案例", level=1,
                region="海淀", expansion_tier=1, type="html_index", max_items=15,
                url="https://zyk.bjhd.gov.cn/ztzl/ylbz/ggcx/",
                item_url_prefix="https://zyk.bjhd.gov.cn/ztzl/ylbz/ggcx/",
                listing_format="haidian_medical")


def medical_item(day, link="./202608/t20260804_test.shtml", target=""):
    return f'''<li><span style="float:right">{day}</span><script>
        var strLink = '{target}';
        if(!strLink){{document.write('<a href="{link}" target="_blank">脱敏案例</a>')}}
        else{{document.write('<a href="'+strLink+'" target="_blank">脱敏案例</a>')}}
        </script></li>'''


def test_haidian_literal_link_and_display_date_without_executing_script():
    html = medical_item("2026-07-08")
    with patch("sqmy.collector.urlopen", side_effect=AssertionError("不得请求正文")):
        nodes = ET.fromstring(listing_to_rss(html + html, medical_source())).findall("item")
    assert len(nodes) == 1
    assert nodes[0].findtext("link").endswith("/202608/t20260804_test.shtml")
    assert nodes[0].findtext("pubDate") == "2026-07-08"
    assert nodes[0].findtext("description") in (None, "")


@pytest.mark.parametrize("html", [
    medical_item("2026-07-08", target="https://other.example/item"),
    medical_item("2026-07-08", link="https://other.example/item"),
    medical_item("2026-07-08").replace("if(!strLink)", "if(false)"),
    medical_item("2026-07-08").replace("var strLink = '';", "var strLink = getLink();"),
])
def test_unknown_or_external_script_branches_not_imported(html):
    with pytest.raises(ValueError, match="目录未解析"):
        listing_to_rss(html, medical_source())


def test_plain_directory_does_not_interpret_scripts():
    source = medical_source()
    source.pop("listing_format")
    with pytest.raises(ValueError, match="目录未解析"):
        listing_to_rss(medical_item("2026-07-08"), source)


def test_official_directory_trial_uses_cache_and_filters_old_dates(tmp_path):
    project = Path(__file__).parents[1]
    (tmp_path / "config").mkdir()
    shutil.copy(project / "config/sources.toml", tmp_path / "config/sources.toml")
    settings = Settings(tmp_path, deepcopy(Settings.load(project / "config/settings.toml").raw))
    collector = SourceCollector(tmp_path, settings.raw)
    source = medical_source()
    collector.sources = [source]
    today = datetime.now(timezone.utc).date()
    html = medical_item(str(today)) + medical_item(str(today - timedelta(days=120)), "./old.shtml")
    cache = tmp_path / "data/cache/indexes" / (hashlib.sha256(source["url"].encode()).hexdigest() + ".xml")
    cache.parent.mkdir(parents=True)
    cache.write_text(html)
    with patch("sqmy.collector.urlopen", side_effect=AssertionError("应复用缓存")):
        items = collector.collect("fixture-directory-trial")
    assert len(items) == 1
    assert items[0].region == "海淀"
    assert items[0].summary == ""
    audit = json.loads((tmp_path / "data/runs/fixture-directory-trial/collection_audit.json").read_text())
    assert audit["sources"][0]["outside_window_count"] == 1


@pytest.mark.parametrize("prefix,ok", [
    ("https://rsj.beijing.gov.cn/", True),
    ("https://rsj.beijing.gov.cn.evil.example/", False),
    ("https://other.example/", False),
    ("https://", False),
])
def test_index_prefix_can_cover_same_site_cross_channel_links(tmp_path, prefix, ok):
    project = Path(__file__).parents[1]
    shutil.copytree(project / "config", tmp_path / "config")
    (tmp_path / "config/sources.toml").write_text(f'''[[sources]]
id="wage"
name="欠薪公告"
type="html_index"
url="https://rsj.beijing.gov.cn/bm/ztzl/gzqx/zc/"
item_url_prefix="{prefix}"
max_items=15
level=1
expansion_tier=2
''')
    settings = Settings(tmp_path, deepcopy(Settings.load(project / "config/settings.toml").raw))
    check = next(x for x in preflight(settings, stage="scan")["checks"] if x["name"] == "discovery_source_config")
    assert check["ok"] is ok
