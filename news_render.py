#!/usr/bin/env python3
"""news_render.py — 把每日 digest 渲染成 GitHub Pages 静态页。

输入: ~/hermes-agent/data/vault_mirror/raw/articles/groundapi/YYYYMMDD_*.md
输出: news/YYYY-MM-DD.html（当日完整版）+ news/index.html（归档索引）

用法:
  python3 news_render.py                 # 渲染所有已有日期 + 更新索引
  python3 news_render.py --date 2026-11-19
  python3 news_render.py --date 2026-11-19 --push   # 渲染后 git commit/push 部署
"""
import argparse
import glob
import html
import os
import re
import subprocess
import sys
from datetime import date as _date

DASH = os.path.expanduser("~/projects/hermes-dashboard")
NEWS_DIR = os.path.join(DASH, "news")
DIGEST_DIR = os.path.expanduser(
    "~/hermes-agent/data/vault_mirror/raw/articles/groundapi"
)
PAGES_BASE = "https://stranger971020.github.io/hermes-dashboard/news"

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import cron_flash971_push as P  # noqa: E402  (复用 digest 解析/打分单一真源)

e = lambda s: html.escape(str(s if s is not None else ""), quote=True)

CSS = """
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"PingFang SC",Segoe UI,sans-serif;background:#0f1115;color:#e6e8ec}
a{color:#6cb6ff;text-decoration:none} a:hover{text-decoration:underline}
header{padding:20px 20px 0;max-width:820px;margin:0 auto}
h1{margin:0;font-size:19px}
.meta{color:#8a8f98;font-size:12px;margin-top:6px}
.meta a{color:#8a8f98}
#q{width:100%;margin:14px 0 0;padding:9px 12px;border-radius:8px;border:1px solid #232733;background:#171a21;color:#e6e8ec;font-size:14px;outline:none}
#q:focus{border-color:#4a9eda}
main{padding:14px 20px 40px;max-width:820px;margin:0 auto}
h2{font-size:13px;color:#aeb4bf;margin:22px 0 8px;letter-spacing:.5px;text-transform:uppercase}
h3{font-size:15px;margin:20px 0 8px;display:flex;align-items:baseline;gap:8px}
h3 .cnt{color:#6f7682;font-weight:400;font-size:12px}
.item{background:#141822;border:1px solid #1d2230;border-radius:9px;padding:10px 12px;margin:7px 0}
.item .t{font-size:14px;font-weight:600;line-height:1.45}
.item .why{color:#9fd0a8;font-size:13px;margin-top:5px}
.item .src{color:#6f7682;font-size:11px;margin-top:5px}
.focus .item{border-color:#2b3d52;background:#16202c}
.focus .item .t{font-size:15px}
footer{padding:10px 20px 40px;max-width:820px;margin:0 auto;color:#8a8f98;font-size:12px;display:flex;gap:16px}
.hidden{display:none}
.day{background:#141822;border:1px solid #1d2230;border-radius:9px;padding:10px 12px;margin:7px 0}
.day .d{font-weight:600}.day .c{color:#8a8f98;font-size:12px;margin-top:3px}
"""

DAY_TMPL = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {iso}</title><style>{css}</style></head><body>
<header>
<h1>📰 Sina 财经/科技早报</h1>
<div class="meta">{iso} · {nthemes} 主题 / {nitems} 条 · <a href="./">归档</a> · <a href="../">看板</a></div>
<input id="q" placeholder="搜索标题 / 关键词 / 解读…" autocomplete="off">
</header>
<main>
{focus}
{themes}
</main>
<footer>{prev}{nxt}<span style="margin-left:auto">全量来源：vault digest</span></footer>
<script>
const q=document.getElementById('q');
const items=[...document.querySelectorAll('.item')];
q.addEventListener('input',()=>{{
  const v=q.value.trim().toLowerCase();
  items.forEach(el=>{{el.classList.toggle('hidden', v && !el.dataset.s.includes(v));}});
}});
</script>
</body></html>"""

INDEX_TMPL = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>早报归档 · Hermes</title><style>{css}</style></head><body>
<header><h1>📰 Sina 财经/科技早报 · 归档</h1>
<div class="meta">共 {n} 天 · <a href="../">返回看板</a></div></header>
<main>{days}</main>
</body></html>"""


def load_day(date8: str):
    """读取某天的全部主题 digest → {date, themes:[{name,declared,rows,insights,meta}]}"""
    files = sorted(glob.glob(os.path.join(DIGEST_DIR, date8 + "_*.md")))
    if not files:
        return None
    themes = []
    for path in files:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        m = P.THEME_HEAD_RE.search(text)
        name = m.group(1).strip() if m else os.path.basename(path)[len(date8) + 1:-3]
        declared = int(m.group(2)) if m else 0

        rows, meta, seen = [], {}, set()
        for title, url, src, tval in P.ROW_META_RE.findall(text):
            title, url = title.strip(), url.strip()
            if not title or not url:
                continue
            rec = meta.setdefault(title, {"sources": 0, "time": None})
            rec["sources"] += 1
            t_dt = P._parse_dt(tval)
            if t_dt and (rec["time"] is None or t_dt > rec["time"]):
                rec["time"] = t_dt
            if title in seen:
                continue
            seen.add(title)
            rows.append((title, url))
        for title, url in P.ROW_RE.findall(text):
            title, url = title.strip(), url.strip()
            if not title or not url or title in seen:
                continue
            seen.add(title)
            rows.append((title, url))
            meta.setdefault(title, {"sources": 1, "time": None})

        themes.append({
            "name": name,
            "declared": declared or len(rows),
            "rows": rows,
            "insights": P._parse_insights(text),
            "meta": meta,
        })
    return {"date": date8, "themes": themes}


def make_scorer(day):
    """score v2: 关键词 + 跨主题重复 + 多源 + 主题权重 + 时间新鲜度 (与推送一致)"""
    counts = {}
    for th in day["themes"]:
        for t, _u in th["rows"]:
            counts[t] = counts.get(t, 0) + 1
    times = [r["time"] for th in day["themes"] for r in th["meta"].values() if r.get("time")]
    ref = max(times) if times else None
    score_title = P._load_score_title()

    def sc(title, theme):
        s = score_title(title)
        s += 2 * max(0, counts.get(title, 1) - 1)
        m = {}
        for th in day["themes"]:
            if title in th["meta"]:
                m = th["meta"][title]
                break
        s += 2 * max(0, m.get("sources", 1) - 1)
        s += P.theme_weight(theme)
        t = m.get("time")
        if t and ref:
            h = (ref - t).total_seconds() / 3600
            s += 2 if h <= 3 else 1 if h <= 6 else 0 if h <= 12 else -1
        return s

    return sc


def _item_html(title, url, reason, meta, cls=""):
    why = f'<div class="why">💡 {e(reason)}</div>' if reason else ""
    bits = []
    if meta.get("time"):
        bits.append(meta["time"].strftime("%H:%M"))
    if meta.get("sources", 1) > 1:
        bits.append(f'{meta["sources"]} 个来源')
    src = f'<div class="src">{" · ".join(bits)}</div>' if bits else ""
    hay = f"{title} {reason or ''}".lower()
    return (
        f'<article class="item {cls}" data-s="{e(hay)}">'
        f'<a class="t" href="{e(url)}" target="_blank" rel="noopener">{e(title)}</a>'
        f"{why}{src}</article>"
    )


def render_day(day):
    iso = f"{day['date'][:4]}-{day['date'][4:6]}-{day['date'][6:]}"
    sc = make_scorer(day)
    nitems = sum(len(th["rows"]) for th in day["themes"])

    # 焦点 Top3 (跨主题去重)
    pool, seen = [], set()
    for th in day["themes"]:
        for t, u in th["rows"]:
            if t in seen:
                continue
            seen.add(t)
            pool.append((t, u, th["name"]))
    pool.sort(key=lambda r: -sc(r[0], r[2]))
    top3 = {(t, u) for t, u, _ in pool[:3]}

    def meta_for(t):
        for th in day["themes"]:
            if t in th["meta"]:
                return th["meta"][t]
        return {}

    def reason_for(t):
        for th in day["themes"]:
            if t in th["insights"]:
                return th["insights"][t]
        return None

    focus_items = "".join(
        _item_html(t, u, reason_for(t), meta_for(t)) for t, u, _ in pool[:3]
    )
    focus = f'<h2>📌 今日焦点</h2><section class="focus">{focus_items}</section>' if focus_items else ""

    theme_html = []
    for th in day["themes"]:
        if not (th["rows"] or th["insights"]):
            continue
        rows = [(t, u) for t, u in th["rows"] if (t, u) not in top3]
        rows.sort(key=lambda r: -sc(r[0], th["name"]))
        items = "".join(_item_html(t, u, reason_for(t), meta_for(t)) for t, u in rows)
        if not items:
            # 无链接条目 (东财格式) → 只展示解读
            items = "".join(
                f'<article class="item"><div class="t">{e(t)}</div>'
                f'<div class="why">💡 {e(r)}</div></article>'
                for t, r in th["insights"].items()
            )
        theme_html.append(
            f'<h3>{e(th["name"])} <span class="cnt">{th["declared"]} 条</span></h3>{items}'
        )

    # 前一天/后一天
    dates = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(NEWS_DIR, "????-??-??.html")))
    nav = []
    if iso in dates:
        i = dates.index(iso)
        if i > 0:
            nav.append(f'<a href="./{dates[i-1]}.html">← {dates[i-1]}</a>')
        if i + 1 < len(dates):
            nav.append(f'<a href="./{dates[i+1]}.html">{dates[i+1]} →</a>')
    prev, nxt = ("".join(nav) + " " if nav else ""), ""

    return DAY_TMPL.format(
        title="Sina 财经/科技早报", iso=iso, nthemes=len(day["themes"]),
        nitems=nitems, css=CSS, focus=focus, themes="".join(theme_html),
        prev=prev, nxt=nxt,
    )


def render_index():
    dates = sorted(
        (os.path.basename(p)[:-5] for p in glob.glob(os.path.join(NEWS_DIR, "????-??-??.html"))),
        reverse=True,
    )
    rows = []
    for iso in dates:
        day = load_day(iso.replace("-", ""))
        if not day:
            continue
        n = sum(len(th["rows"]) for th in day["themes"]) or sum(th["declared"] for th in day["themes"])
        nt = len(day["themes"])
        rows.append(
            f'<a class="day" href="./{iso}.html" style="display:block">'
            f'<div class="d">{iso}</div><div class="c">{nt} 主题 / {n} 条</div></a>'
        )
    return INDEX_TMPL.format(css=CSS, n=len(rows), days="".join(rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (默认: 全部)")
    ap.add_argument("--push", action="store_true", help="渲染后 git commit/push")
    args = ap.parse_args()

    os.makedirs(NEWS_DIR, exist_ok=True)
    if args.date:
        date8 = args.date.replace("-", "")
        day = load_day(date8)
        if not day:
            print(f"no digest for {args.date}", file=sys.stderr)
            return 1
        days = [day]
    else:
        dates = sorted({os.path.basename(p)[:8] for p in glob.glob(os.path.join(DIGEST_DIR, "????????_*.md"))})
        days = [d for d in (load_day(x) for x in dates) if d]

    for day in days:
        iso = f"{day['date'][:4]}-{day['date'][4:6]}-{day['date'][6:]}"
        out = os.path.join(NEWS_DIR, iso + ".html")
        open(out, "w", encoding="utf-8").write(render_day(day))
        print(f"rendered {out}")

    idx = os.path.join(NEWS_DIR, "index.html")
    open(idx, "w", encoding="utf-8").write(render_index())
    print(f"rendered {idx}")

    if args.push:
        subprocess.run(["git", "add", "news"], cwd=DASH, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=DASH).returncode != 0:
            subprocess.run(["git", "commit", "-q", "-m", "news: daily briefing pages"], cwd=DASH, check=True)
            subprocess.run(["git", "push", "-q", "origin", "main"], cwd=DASH, check=True)
            print("pushed to GitHub Pages")
        else:
            print("no changes to push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
