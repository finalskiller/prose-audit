#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_metrics.py — 语言审查的确定性初筛。

只数数，不判对错：句长分布、可疑短句、超长句、重复短语、套话、连接词密度、
半角标点、中英混排、括号嵌套、缩写展开情况。判定由审稿人按
references/rubric.md 做，脚本的输出只是线索。

用法:
    python audit_metrics.py <文件路径>
    python audit_metrics.py <文件路径> --json
    python audit_metrics.py -            # 标准输入（按 UTF-8 解码）

注意：Windows 下推荐用文件输入，管道输入受控制台编码影响。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter

CJK = r'\u4e00-\u9fff'

FENCE_RE = re.compile(r'```.*?```', re.S)
INLINE_CODE_RE = re.compile(r'`[^`\n]*`')
FRONTMATTER_RE = re.compile(r'^---\r?\n.*?\r?\n---\r?\n', re.S)

# 标题、列表、表格、引用行不进短句检测（D3 对这些体裁豁免）
SKIP_LINE_RE = re.compile(r'^\s*(?:#{1,6}\s|[-*·•]\s+|\d+[.、)]\s+|>|\|)')

SENTENCE_END = '。！？!?…'
# 中文句末标点一律切；英文句点只在字母数字之后、且后接空白或行尾时切，避免切坏 3.5 和 v1.2
SENT_END_RE = re.compile(r'[。！？!?…]|(?<=[A-Za-z0-9])\.(?=\s|$)')

SUBJECT_HINTS = (
    '我', '你', '您', '他', '她', '它', '咱', '大家',
    '这', '那', '该', '其', '此', '本', '谁', '哪',
)

# 真实缺主谓的常见开头（比"含不含代词"可靠得多）
FRAGMENT_START_RE = re.compile(r'^(?:已|待|无|未|暂无|建议|需|可以|确认|同意|收到|已知)')

CONNECTORS = (
    '首先', '其次', '再者', '最后', '然后', '接下来',
    '因此', '所以', '此外', '同时', '另外', '综上',
    '值得注意', '不仅', '而且', '更重要的是', '一方面', '另一方面',
    '总而言之', '简而言之', '换句话说', '事实上', '实际上',
)

FILLERS = {
    '套话过渡': r'值得一提的是|众所周知|不难发现|毋庸置疑|不得不说|如前所述',
    '空泛收口': r'总的来说|总体而言|总而言之|简而言之|综合来看|整体来看',
    '同义叠加': r'(非常|十分|极其|特别).{0,4}(重要|关键|核心|重视)',
    '程度虚词': r'基本上可以说|可以说是|某种程度上|在一定程度上|在某种意义上',
    '三段模板': r'首先[，,].{0,40}其次[，,].{0,40}最后',
    '双层转折': r'不仅.{0,15}而且.{0,15}更',
}

BIGRAM_STOP = {
    '我们', '你们', '他们', '这个', '那个', '一个', '可以', '需要',
    '已经', '就是', '但是', '因为', '所以', '如果', '没有', '不是',
    '还是', '这些', '那些', '什么', '怎么', '现在', '目前',
    '时候', '问题', '这样', '那样', '自己', '一下', '进行',
}


# ---------- 输入 ----------

def read_bytes(path: str) -> bytes:
    if path == '-':
        return sys.stdin.buffer.read()
    with open(path, 'rb') as fh:
        return fh.read()


def decode(data: bytes) -> tuple[str, str]:
    """依次尝试 UTF-8(BOM)、UTF-8、GB18030。"""
    for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ValueError('无法解码：文件既不是 UTF-8 也不是 GB18030')


# ---------- 清洗 ----------

def strip_code(text: str) -> tuple[str, bool]:
    unclosed = text.count('```') % 2 == 1
    text = FENCE_RE.sub(lambda m: '\n' * m.group(0).count('\n') or ' ', text)
    text = INLINE_CODE_RE.sub(' ', text)
    return text, unclosed


def strip_frontmatter(text: str) -> str:
    text = text.lstrip('\ufeff')
    m = FRONTMATTER_RE.match(text)
    return text[m.end():] if m else text


def prose_lines(text: str) -> tuple[list[str], int]:
    kept, skipped = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if SKIP_LINE_RE.match(line):
            skipped += 1
            continue
        kept.append(line)
    return kept, skipped


def split_sentences(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        start = 0
        for m in SENT_END_RE.finditer(line):
            seg = line[start:m.end()].strip()
            if seg and re.search(r'\w', seg):
                out.append(seg)
            start = m.end()
        tail = line[start:].strip()
        if tail:
            out.append(tail)
    return out


def visible_len(s: str) -> int:
    return len(re.sub(r'\s', '', s))


# ---------- 检测 ----------

def find_fragments(sentences: list[str]) -> list[str]:
    found = []
    for s in sentences:
        core = s.rstrip(SENTENCE_END + '.，,、；;')
        if visible_len(core) > 10:
            continue
        if any(h in core for h in SUBJECT_HINTS):
            continue
        if not FRAGMENT_START_RE.match(core):
            continue
        if not re.search(f'[{CJK}]', core):
            continue
        found.append(s)
    return found


def _overlap(a: str, b: str) -> int:
    """最长公共子串长度，用来识别同一处重复被滑窗切出的多个近亲。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > best:
                best = k
    return best


def repeated_phrases(text: str, n_min: int, n_max: int, top: int) -> list[dict]:
    clean = re.sub(rf'[^{CJK}]', '|', text)
    counts: Counter = Counter()
    for chunk in clean.split('|'):
        if len(chunk) < n_min:
            continue
        for n in range(n_min, min(n_max, len(chunk)) + 1):
            for i in range(len(chunk) - n + 1):
                counts[chunk[i:i + n]] += 1

    candidates = {g: c for g, c in counts.items() if c >= 2 and not (len(g) == 2 and g in BIGRAM_STOP)}
    if not candidates:
        return []

    ranked = sorted(candidates.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    final: list[tuple[str, int]] = []
    for gram, count in ranked:
        duplicate = False
        for kept, kept_count in final:
            if gram in kept or kept in gram:
                duplicate = True
                break
            # 同频且高度重叠：同一处重复的滑窗近亲，只留一条
            if count == kept_count and _overlap(gram, kept) >= min(len(gram), len(kept)) - 1:
                duplicate = True
                break
        if duplicate:
            continue
        final.append((gram, count))
        if len(final) >= top:
            break
    return [{'phrase': g, 'count': c} for g, c in final]


def filler_hits(text: str) -> list[dict]:
    hits = []
    for label, pattern in FILLERS.items():
        matches = re.findall(pattern, text)
        if matches:
            sample = matches[0] if isinstance(matches[0], str) else ''.join(matches[0])
            hits.append({'label': label, 'count': len(matches), 'sample': sample[:30]})
    return hits


def count_connector(text: str, word: str) -> int:
    if word == '所以':
        return len(re.findall(r'(?<!之)所以', text))
    return text.count(word)


def connector_density(text: str) -> dict:
    hits = {w: count_connector(text, w) for w in CONNECTORS}
    hits = {k: v for k, v in hits.items() if v}
    total = sum(hits.values())
    chars = max(visible_len(text), 1)
    return {
        'total': total,
        'per_100_chars': round(total * 100 / chars, 2),
        'items': dict(sorted(hits.items(), key=lambda kv: -kv[1])),
    }


def _adjacent_count(text: str, ch: str) -> int:
    e = re.escape(ch)
    return len(re.findall(rf'(?<=[{CJK}]){e}|{e}(?=[{CJK}])', text))


def punctuation_report(text: str, sentence_count: int) -> dict:
    halfwidth = {
        '逗号': _adjacent_count(text, ','),
        '句点': len(re.findall(rf'(?<=[{CJK}])\.', text)),
        '冒号': _adjacent_count(text, ':'),
        '分号': _adjacent_count(text, ';'),
        '括号': _adjacent_count(text, '(') + _adjacent_count(text, ')'),
        '问号': _adjacent_count(text, '?'),
        '叹号': _adjacent_count(text, '!'),
    }
    dash = len(re.findall(r'——|—', text))
    ellipsis = len(re.findall(r'……|…', text))
    fullwidth = {
        '逗号': text.count('，'),
        '句点': text.count('。'),
        '冒号': text.count('：'),
        '分号': text.count('；'),
        '破折号': dash,
        '省略号': ellipsis,
        '括号': text.count('（'),
    }
    return {
        'halfwidth_in_cjk': {k: v for k, v in halfwidth.items() if v},
        'fullwidth': {k: v for k, v in fullwidth.items() if v},
        'nested_parentheses': len(re.findall(r'（[^（）]{0,20}（', text)),
        'dashes_per_sentence': round(dash / max(sentence_count, 1), 2),
    }


def mixed_spacing(text: str) -> dict:
    before = re.findall(rf'[{CJK}][A-Za-z0-9]', text)
    after = re.findall(rf'[A-Za-z0-9][{CJK}]', text)
    samples = list(dict.fromkeys(before + after))[:8]
    return {'count': len(before) + len(after), 'samples': samples}


def abbreviations(text: str) -> list[dict]:
    tokens = re.findall(r'(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,5}(?![A-Za-z0-9])', text)
    seen: Counter = Counter(tokens)
    out = []
    for token, count in seen.most_common():
        explained = bool(re.search(rf'{token}\s*[（(]', text)) or bool(
            re.search(rf'[（(][^）)]{{0,20}}{token}', text))
        out.append({'token': token, 'count': count, 'explained': explained})
    return out


def list_markers(text: str) -> dict:
    bullets = len(re.findall(r'^\s*[-*·•]\s+', text, re.M))
    numbered = len(re.findall(r'^\s*\d+[.、)]\s+', text, re.M))
    return {'bullet': bullets, 'numbered': numbered, 'mixed': bool(bullets and numbered)}


# ---------- 主流程 ----------

def analyse(raw: str, n_min: int = 3, top: int = 8) -> dict:
    text, unclosed = strip_code(strip_frontmatter(raw))
    lines, skipped = prose_lines(text)
    sentences = split_sentences(lines)
    lengths = [visible_len(s) for s in sentences]
    total_chars = sum(lengths)

    cv = 0.0
    if len(lengths) >= 5 and statistics.mean(lengths) > 0:
        cv = round(statistics.pstdev(lengths) / statistics.mean(lengths), 2)

    return {
        'overview': {
            'chars': total_chars,
            'sentences': len(sentences),
            'avg_sentence_len': round(total_chars / len(sentences), 1) if sentences else 0,
            'max_sentence_len': max(lengths) if lengths else 0,
            'sentence_len_cv': cv,
            'uniform_rhythm': len(lengths) >= 5 and cv < 0.25,
            'skipped_lines': skipped,
            'unclosed_code_fence': unclosed,
        },
        'fragments': find_fragments(sentences),
        'long_sentences': [
            {'text': s[:80], 'len': visible_len(s)}
            for s in sentences if visible_len(s) > 60
        ],
        'repeated_phrases': repeated_phrases(text, n_min, max(6, n_min), top),
        'fillers': filler_hits(text),
        'connectors': connector_density(text),
        'punctuation': punctuation_report(text, len(sentences)),
        'mixed_spacing': mixed_spacing(text),
        'abbreviations': abbreviations(text),
        'list_markers': list_markers(text),
    }


def render(r: dict) -> str:
    o = r['overview']
    out = [
        '== 概述 ==',
        f"字数 {o['chars']} ｜ 句数 {o['sentences']} ｜ 平均句长 {o['avg_sentence_len']} ｜ "
        f"最长 {o['max_sentence_len']} 字",
    ]
    if o['skipped_lines']:
        out.append(f"已跳过 {o['skipped_lines']} 行标题/列表/表格（这些体裁不判 D3）")
    if o['unclosed_code_fence']:
        out.append('⚠ 代码围栏不配对，代码可能被当成正文')
    if o['chars'] == 0:
        out.append('⚠ 没有可分析的正文（可能整篇都是代码或为空）')
    if o['uniform_rhythm']:
        out.append(f"⚠ 句长变异系数 {o['sentence_len_cv']}（<0.25），节奏过于均匀")

    out += ['', f"== 可疑短句（≤10 字，仅提示：{len(r['fragments'])}）=="]
    out += [f'  · {s}' for s in r['fragments']] or ['  （无）']

    if r['long_sentences']:
        out += ['', f"== 超长句（>60 字：{len(r['long_sentences'])}）=="]
        out += [f"  · {x['len']} 字：{x['text']}" for x in r['long_sentences']]

    out += ['', f"== 重复短语（候选，可能是话题词：{len(r['repeated_phrases'])}）=="]
    out += [f"  · 「{x['phrase']}」×{x['count']}" for x in r['repeated_phrases']] or ['  （无）']

    out += ['', f"== 套话与空转句式（{len(r['fillers'])} 类）=="]
    if r['fillers']:
        out += [f"  · {f['label']} ×{f['count']}　例：{f['sample']}" for f in r['fillers']]
    else:
        out.append('  （无）')

    c = r['connectors']
    out += ['', f"== 连接词 ==\n  共 {c['total']} 处，每百字 {c['per_100_chars']} 处"]
    if c['items']:
        out.append('  ' + '　'.join(f'{k}×{v}' for k, v in c['items'].items()))

    p = r['punctuation']
    out += ['', '== 标点 ==']
    if p['halfwidth_in_cjk']:
        out.append('  半角混入：' + '　'.join(f'{k} {v}' for k, v in p['halfwidth_in_cjk'].items()))
    else:
        out.append('  半角混入：无')
    if p['nested_parentheses']:
        out.append(f"  括号嵌套：{p['nested_parentheses']} 处")
    if p['dashes_per_sentence']:
        out.append(f"  破折号：{p['dashes_per_sentence']} 个/句")

    m = r['mixed_spacing']
    out += ['', f"== 中英数字混排（缺空格 {m['count']} 处，最多扣 1 分）=="]
    out += [f'  · {s}' for s in m['samples']] or ['  （无）']

    out += ['', f"== 缩写（{len(r['abbreviations'])}）=="]
    if r['abbreviations']:
        for a in r['abbreviations']:
            out.append(f"  · {a['token']}×{a['count']}" + ('' if a['explained'] else '  ← 未见展开'))
    else:
        out.append('  （无）')

    lm = r['list_markers']
    if lm['mixed']:
        out += ['', '== 列表符号混用 ==', f"  项目符号 {lm['bullet']} 处，编号 {lm['numbered']} 处"]

    out += ['', '提示：以上均为计数线索，不等于缺陷。判定见 references/rubric.md。']
    return '\n'.join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='audit_metrics.py',
        description='语言审查的确定性初筛：只数数，不判对错。',
    )
    p.add_argument('source', help='文本文件路径，或 - 表示标准输入')
    p.add_argument('--json', action='store_true', help='输出 JSON')
    p.add_argument('--min-phrase', type=int, default=3, help='重复短语最短长度，2–6，默认 3')
    p.add_argument('--top', type=int, default=8, help='重复短语条数上限，1–50，默认 8')
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if not 2 <= args.min_phrase <= 6:
        print('error: --min-phrase must be an integer between 2 and 6', file=sys.stderr)
        return 2
    if not 1 <= args.top <= 50:
        print('error: --top must be an integer between 1 and 50', file=sys.stderr)
        return 2

    try:
        data = read_bytes(args.source)
    except OSError as err:
        print(f'error: cannot read file: {err}', file=sys.stderr)
        return 2

    try:
        raw, encoding = decode(data)
    except ValueError as err:
        print(f'error: cannot decode file: {err}', file=sys.stderr)
        return 2

    if not raw.strip():
        if args.json:
            print(json.dumps({'error': '输入为空'}, ensure_ascii=False))
        else:
            print('error: empty input', file=sys.stderr)
        return 2

    result = analyse(raw, n_min=args.min_phrase, top=args.top)
    result['source'] = {'path': args.source, 'encoding': encoding}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

