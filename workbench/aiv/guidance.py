"""Versioned recommendations for assisted human review, never gold labels."""
import re

REASON_FIELDS = ('主要操作', '对象与条件', '判定依据', '边界与不确定性')
PLACEHOLDER_PATTERN = r'【待补充：[^】]*】'
REASON_TEMPLATE = '\n'.join(f'{field}：【待补充：{field}】' for field in REASON_FIELDS)


def validate_guidance(guidance, question):
    if not isinstance(guidance, dict):
        raise ValueError('推荐答案结构无效')
    label = guidance.get('label')
    if label is not None and (type(label) is not int or not 1 <= label <= 6):
        raise ValueError('推荐层级无效')
    if 'label' not in guidance:
        raise ValueError('推荐答案缺少层级')
    evidence = guidance.get('evidence')
    if not isinstance(evidence, str) or not evidence or evidence not in question:
        raise ValueError('推荐证据必须为当前题目的连续原文')
    parts = guidance.get('reason_parts', {})
    for field in REASON_FIELDS:
        if not isinstance(parts.get(field), str) or not parts[field].strip():
            raise ValueError('推荐理由字段不完整')
    reason = '\n'.join(f'{field}：{parts[field].strip()}' for field in REASON_FIELDS)
    if len(reason) > 1000 or re.search(PLACEHOLDER_PATTERN, reason):
        raise ValueError('推荐理由尚未填写或超过字数限制')
    for field in ('version', 'author', 'note'):
        if not isinstance(guidance.get(field), str) or not guidance[field].strip():
            raise ValueError('推荐答案缺少版本或来源说明')
    return {key: guidance[key] for key in ('version', 'author', 'note', 'label', 'evidence')} | {
        'reason_parts': {field: parts[field].strip() for field in REASON_FIELDS},
        'reason': reason, 'reason_template': REASON_TEMPLATE,
        'boundary_case': bool(guidance.get('boundary_case', False)),
    }
