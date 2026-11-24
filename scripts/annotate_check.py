#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""annotate_check.py — 标注结果的机械校验与候选锚点。

两种用法：
    python annotate_check.py --payload payload.json --candidates
    python annotate_check.py --payload payload.json --answer answer.json [--json]

payload.json:
    {"context": "...", "replies": {"1": "...", "2": "..."}}

answer.json: 标注输出的 JSON（见 SKILL.md）。

只做机械可判的事：引文是否逐字、区间是否对得上、数字有没有丢、强度词有没有被偷偷加强、
修正稿是不是空壳。判断质量不归它管。
"""

from __future__ import annotations

import argparse
import json
import re
import sys

CJK = r'\u4e00-\u9fff'
SENT_END_RE = re.compile(r'[。！？!?…]|(?<=[A-Za-z0-9])\.(?=\s|$)')

KINDS = {f'D{i}' for i in range(1, 13)}
SCOPES = {'span', 'sentence', 'paragraph'}

STRENGTHENERS = ('一定', '保证', '已经', '全部', '彻底', '必然', '绝对', '完全',
                 '确保', '肯定', '必须', '从未', '从不', '立即', '马上')
NEGATORS = '不没未无'
HEDGES = ('预计', '可能', '大概', '部分', '仅', '至少', '大约', '左右', '也许', '或许')

NUM_RE = re.compile(r'\d+(?:[.,]\d+)*(?:\s?(?:%|ms|秒|分钟|小时|天|周|个月|年|个|次|条|台|人))?')
# 只认带大写/下划线/数字/连字符的英文标识符，避免把 key、the 这类普通词当专名
ID_RE = re.compile(r'(?<![A-Za-z0-9])(?=[A-Za-z0-9_.\-]{3,})(?=[A-Za-z0-9_.\-]*[A-Z0-9_\-])[A-Za-z][A-Za-z0-9_.\-]{2,}(?![A-Za-z0-9])')

FILLERS = re.compile(
    r'值得一提的是|众所周知|不难发现|毋庸置疑|总的来说|总体而言|总而言之|简而言之'
    r'|基本上可以说|可以说是|某种程度上|在一定程度上|在某种意义上'
)
HALF_RE = re.compile(rf'(?<=[{CJK}])[,;:!?]|[,;:!?](?=[{CJK}])')
FRAG_START = re.compile(r'^(?:已|待|无|未|暂无|建议|需|可以|确认|同意|收到|已知)')
SUBJECT_HINTS = ('我', '你', '您', '他', '她', '它', '咱', '大家', '这', '那', '该', '其', '此', '本')


def load_json(path: str) -> dict:
    with open(path, encoding='utf-8-sig') as fh:
        raw = fh.read().strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\s*\n', '', raw)
        raw = re.sub(r'\n```\s*$', '', raw)
    return json.loads(raw)


def visible_len(s: str) -> int:
    return len(re.sub(r'\s', '', s))


def sentences_with_offsets(text: str) -> list[tuple[int, int]]:
    out = []
    start = 0
    for m in SENT_END_RE.finditer(text):
        if text[start:m.end()].strip():
            out.append((start, m.end()))
        start = m.end()
    if text[start:].strip():
        out.append((start, len(text)))
    return out


def candidates_for(text: str, model: str) -> list[dict]:
    out = []
    for a, b in sentences_with_offsets(text):
        seg = text[a:b]
        core = seg.strip().rstrip('。！？!?…，,；;')
        if visible_len(seg) > 60:
            out.append({'model': model, 'start': a, 'end': b, 'type': '长句',
                        'text': seg[:40]})
        if (visible_len(core) <= 10 and FRAG_START.match(core)
                and not any(h in core for h in SUBJECT_HINTS)):
            out.append({'model': model, 'start': a, 'end': b, 'type': '可疑短句',
                        'text': seg[:40]})
    for m in FILLERS.finditer(text):
        out.append({'model': model, 'start': m.start(), 'end': m.end(),
                    'type': '套话', 'text': m.group(0)})
    for m in HALF_RE.finditer(text):
        out.append({'model': model, 'start': m.start(), 'end': m.end(),
                    'type': '半角标点', 'text': m.group(0)})
    for m in ID_RE.finditer(text):
        token = m.group(0)
        if re.search(rf'{token}\s*[（(]', text):
            continue
        out.append({'model': model, 'start': m.start(), 'end': m.end(),
                    'type': '缩写', 'text': token})
    out.sort(key=lambda x: (x['model'], x['start']))
    return out


def tokens(text: str) -> set[str]:
    """抽出数字与英文标识符。单位数字（列表序号）不算数据，不参与丢失检查。"""
    out: set[str] = set()
    for m in NUM_RE.finditer(text):
        tok = m.group(0)
        digits = re.sub(r'\D', '', tok)
        has_unit = bool(re.search(r'[%m秒分钟小时天周月个次条台人]', tok))
        if has_unit or len(digits) >= 2:
            out.add(tok)
    out |= {m.group(0) for m in ID_RE.finditer(text)}
    return out


def count_strong(word: str, text: str) -> int:
    """带否定的强度词不算（"不一定"不是"一定"）。"""
    return len(re.findall(rf'(?<![{NEGATORS}]){re.escape(word)}', text))


def norm(text: str) -> str:
    return re.sub(r'\s', '', text)


def overlaps(a1: int, a2: int, b1: int, b2: int) -> bool:
    return max(a1, b1) < min(a2, b2)


def check(payload: dict, answer: dict) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warns: list[str] = []
    infos: list[str] = []

    context = payload.get('context', '')
    replies = {str(k): v for k, v in payload.get('replies', {}).items()}
    if not replies:
        errors.append('payload 里没有 replies')

    # --- 结构 ---
    for field in ('coverage', 'spans', 'corrected_reply'):
        if field not in answer:
            errors.append(f'缺少字段 {field}')
    if not isinstance(answer.get('spans'), list):
        errors.append('spans 必须是数组')
        answer['spans'] = []

    cov = answer.get('coverage', {}) or {}
    asked = (cov.get('asked') or {}).get('quote', '')
    if not asked:
        errors.append('coverage.asked.quote 为空')
    elif context and norm(asked) not in norm(context):
        errors.append(f'asked.quote 不是上下文原文：{asked[:30]}')

    quality = cov.get('quality', {}) or {}
    for m in replies:
        q = quality.get(m) or quality.get(int(m)) if m.isdigit() else quality.get(m)
        if not isinstance(q, dict):
            errors.append(f'coverage.quality.{m} 缺失')
            continue
        label = q.get('label')
        if label not in ('ok', 'low'):
            errors.append(f'quality.{m}.label 非法：{label}')
        if not q.get('reason'):
            errors.append(f'quality.{m}.reason 为空')
        if label == 'low' and not (q.get('missing') or []):
            warns.append(f'quality.{m} 判 low 但没有 missing 项')

    # --- span ---
    for i, sp in enumerate(answer.get('spans', [])):
        tag = f'spans[{i}]'
        m = str(sp.get('model'))
        if m not in replies:
            errors.append(f'{tag}.model 非法：{sp.get("model")}')
            continue
        text = replies[m]
        start, end = sp.get('start'), sp.get('end')
        if not isinstance(start, int) or not isinstance(end, int):
            errors.append(f'{tag} 缺 start/end')
            continue
        if not (0 <= start < end <= len(text)):
            errors.append(f'{tag} 区间越界：{start}-{end}（回复长 {len(text)}）')
            continue
        if text[start:end] != sp.get('quote'):
            errors.append(f'{tag} quote 与区间不一致：期望「{text[start:end][:20]}」'
                          f'，实际「{str(sp.get("quote"))[:20]}」')
        if sp.get('kind') not in KINDS:
            errors.append(f'{tag}.kind 非法：{sp.get("kind")}')
        if sp.get('scope') not in SCOPES:
            errors.append(f'{tag}.scope 非法：{sp.get("scope")}')
        if not sp.get('why'):
            errors.append(f'{tag}.why 为空')
        if not sp.get('fix'):
            errors.append(f'{tag}.fix 为空')
        if visible_len(sp.get('quote', '')) > 60:
            warns.append(f'{tag} 引文 {visible_len(sp["quote"])} 字，超过 60')

    # --- flags / conflicts ---
    for i, fl in enumerate(answer.get('flags', []) or []):
        m = str(fl.get('model'))
        q = fl.get('quote', '')
        if m in replies and q and norm(q) not in norm(replies[m]):
            errors.append(f'flags[{i}].quote 不是回复原文')
    for i, cf in enumerate(answer.get('conflicts', []) or []):
        for k in ('quote_1', 'quote_2'):
            q = cf.get(k, '')
            if q and not any(norm(q) in norm(r) for r in replies.values()):
                errors.append(f'conflicts[{i}].{k} 不是任何一份回复的原文')
        if cf.get('resolution') not in ('pick_1', 'pick_2', 'both', 'unknown'):
            errors.append(f'conflicts[{i}].resolution 非法')

    # --- 修正稿 ---
    cr = answer.get('corrected_reply', {}) or {}
    text = cr.get('text', '') or ''
    if not text.strip():
        errors.append('corrected_reply.text 为空')
        return errors, warns, infos
    if cr.get('based_on') not in ('1', '2', 'merged', 1, 2):
        errors.append(f'corrected_reply.based_on 非法：{cr.get("based_on")}')

    sources = {'context': context, 'reply_1': replies.get('1', ''), 'reply_2': replies.get('2', '')}
    for i, ad in enumerate(cr.get('added', []) or []):
        src = ad.get('source')
        q = ad.get('quote', '')
        if src not in sources:
            errors.append(f'added[{i}].source 非法：{src}')
        elif q and norm(q) not in norm(sources[src]):
            errors.append(f'added[{i}].quote 不在声明的来源里')

    dropped_text = ' '.join(d.get('text', '') for d in (cr.get('dropped', []) or []))
    combined = norm(text) + norm(dropped_text)
    based = str(cr.get('based_on'))
    if based == 'merged':
        required = tokens(replies.get('1', '')) | tokens(replies.get('2', ''))
        optional: set[str] = set()
    else:
        required = tokens(replies.get(based, ''))
        optional = (tokens(replies.get('1', '')) | tokens(replies.get('2', ''))) - required
    for token in sorted(required):
        if norm(token) not in combined:
            errors.append(f'数字/标识符丢失且未声明（来自底稿 {based}）：{token}')
    for token in sorted(optional):
        if norm(token) not in combined:
            infos.append(f'底稿里没有的标识符未保留（可接受）：{token}')

    for w in STRENGTHENERS:
        src_max = max([count_strong(w, r) for r in replies.values()] or [0])
        out_count = count_strong(w, text)
        if out_count > src_max:
            errors.append(f'强度词被加强：{w}（原文 {src_max} 次，修正稿 {out_count} 次）')
    for w in HEDGES:
        src_max = max([r.count(w) for r in replies.values()] or [0])
        if src_max > 0 and text.count(w) < src_max:
            warns.append(f'限定词减少：{w}（原文 {src_max} 次，修正稿 {text.count(w)} 次）')

    if replies:
        floor = min(len(r) for r in replies.values())
        ratio = len(text) / max(floor, 1)
        if ratio < 0.4:
            errors.append(f'修正稿疑似空壳：长度只有最短原文的 {ratio:.0%}')
        elif ratio < 0.6:
            warns.append(f'修正稿偏短：长度是最短原文的 {ratio:.0%}')

    # --- 锚点覆盖（只提示） ---
    anchors = []
    for m, r in replies.items():
        anchors.extend(candidates_for(r, m))
    if anchors:
        hit = sum(1 for sp in answer.get('spans', [])
                  if any(str(sp.get('model')) == str(a['model'])
                         and overlaps(sp.get('start', 0), sp.get('end', 0), a['start'], a['end'])
                         for a in anchors))
        total = len(answer.get('spans', [])) or 1
        infos.append(f'锚点覆盖：{hit}/{total} 个 span 落在候选附近')
        if hit == 0 and answer.get('spans'):
            warns.append('没有任何 span 落在候选锚点附近，确认不是漏标机械信号')

    return errors, warns, infos


def render_candidates(payload: dict) -> str:
    lines = []
    for m, r in payload.get('replies', {}).items():
        lines.append(f'== 模型 {m} 的候选锚点 ==')
        cands = candidates_for(r, str(m))
        if not cands:
            lines.append('  （无）')
        for c in cands:
            lines.append(f"  [{c['start']:>3}-{c['end']:<3}] {c['type']:<6} {c['text']}")
        lines.append('')
    lines.append('候选只是核对清单：采纳要写清为什么，不采纳也要能说出为什么。')
    lines.append('真正伤读者的语义病（D2/D7/D8/D12）机器给不了，得靠冷读。')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    p = argparse.ArgumentParser(prog='annotate_check.py')
    p.add_argument('--payload', required=True, help='上下文与两份回复')
    p.add_argument('--answer', help='标注结果 JSON')
    p.add_argument('--candidates', action='store_true', help='只输出候选锚点')
    p.add_argument('--json', action='store_true')
    args = p.parse_args(argv)

    try:
        payload = load_json(args.payload)
    except (OSError, json.JSONDecodeError) as err:
        print(f'error: 读不了 payload：{err}', file=sys.stderr)
        return 2

    if args.candidates:
        print(render_candidates(payload))
        return 0

    if not args.answer:
        print('error: 需要 --answer 或 --candidates', file=sys.stderr)
        return 2

    try:
        answer = load_json(args.answer)
    except (OSError, json.JSONDecodeError) as err:
        print(f'error: 读不了 answer：{err}', file=sys.stderr)
        return 2

    errors, warns, infos = check(payload, answer)

    if args.json:
        print(json.dumps({'errors': errors, 'warnings': warns, 'info': infos},
                         ensure_ascii=False, indent=2))
    else:
        for w in warns:
            print(f'[warn] {w}')
        for i in infos:
            print(f'[info] {i}')
        for e in errors:
            print(f'[FAIL] {e}')
        print(f"\n结果：{len(errors)} 处错误，{len(warns)} 处警告")

    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())

