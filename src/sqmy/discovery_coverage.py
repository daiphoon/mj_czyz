"""发现渠道与材料身份分列；只观察，不参与排序或证据判断。"""
from collections import Counter
from urllib.parse import urlparse


def provenance(url, *, channel, source_id, material_kind='unclassified',
               date_basis='publisher_date', query_id=None, query_role=None):
    return dict(channel=channel, publisher_domain=(urlparse(url).hostname or ''),
                collection_id=source_id, material_kind=material_kind, date_basis=date_basis,
                query_id=query_id, query_role=query_role, original_chain_status='not_verified')


def coverage_summary(events, collection_stats):
    unique = {event.url: event for event in events}
    dimensions = {key: Counter() for key in ('channel', 'material_kind', 'region', 'source_level', 'publisher')}
    for event in unique.values():
        info = event.material.get('discovery_provenance', {})
        dimensions['channel'][info.get('channel', 'legacy_unknown')] += 1
        dimensions['material_kind'][info.get('material_kind', 'unclassified')] += 1
        dimensions['publisher'][info.get('publisher_domain') or urlparse(event.url).hostname or 'unknown'] += 1
        dimensions['region'][event.region] += 1
        dimensions['source_level'][str(event.source_level)] += 1
    return dict(unique_urls=len(unique),
                **{'by_' + key: dict(sorted(value.items())) for key, value in dimensions.items()},
                endpoint_skipped=sum(row.get('request_status') == 'skipped_endpoint_unavailable' for row in collection_stats),
                notice='按URL去重统计；材料类型来自已核验栏目或为未分类，不认证事实。不同域名/渠道不等于独立信息链，旧四层不代表信源等级。')
