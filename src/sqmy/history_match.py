"""轻量机制重叠提示；字符相似不能证明政策建议重复。"""
import re


def _grams(text):
    text = re.sub('审核|审查|校验|复核', '核验', text)
    chunks = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text.lower())
    return {chunk[i:i + 2] for chunk in chunks for i in range(len(chunk) - 1)}


def mechanism_hints(text, rows, config):
    query = _grams(text)
    matches = []
    for row in rows:
        subject = _grams((row['title'] or '') + (row['affected_group'] or ''))
        fields = [_grams(row[key] or '') for key in ('core_problem', 'mechanism_entry', 'recommendation_summary')]
        mechanism = set().union(*fields)
        shared_subject, shared_mechanism = query & subject, query & mechanism
        subject_ratio = len(shared_subject) / max(1, len(subject))
        mechanism_ratio = max(len(query & field) / max(1, len(field)) for field in fields)
        if (len(shared_subject) >= config['min_shared_subject']
                and len(shared_mechanism) >= config['min_shared_mechanism']
                and subject_ratio >= config['min_subject_ratio']
                and mechanism_ratio >= config['min_mechanism_ratio']):
            matches.append(dict(topic_id=row['id'], title=row['title'],
                                mechanism=(row['mechanism_entry'] or '')[:config['summary_chars']],
                                shared_terms=sorted(shared_mechanism)[:12],
                                overlap=round(mechanism_ratio, 3),
                                instruction='需比较群体、事实变化及建议机制；不是自动排除结论'))
    return sorted(matches, key=lambda x: -x['overlap'])[:config['max_hints']]
