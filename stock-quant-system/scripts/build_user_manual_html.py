"""从 用户使用说明书.md 生成可翻页、可检索、带目录/索引的单文件 HTML。"""
from __future__ import annotations

import base64
import html
import json
import re
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "manuals" / "用户使用说明书.md"
OUT = ROOT / "docs" / "manuals" / "用户使用说明书.html"
ASSETS = ROOT / "docs" / "manuals" / "assets"


def slugify(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"^\d+(\.\d+)*\s*", "", t)
    t = t.replace(" ", "-")
    return t


def parse_pages(md: str) -> list[dict]:
    lines = md.replace("\r\n", "\n").split("\n")
    pages: list[dict] = []
    chapter = "封面"
    title = "封面"
    buf: list[str] = []
    pid = 0

    def flush() -> None:
        nonlocal pid, buf, title, chapter
        body = "\n".join(buf).strip()
        if not body:
            buf = []
            return
        pages.append(
            {
                "id": f"p{pid}",
                "chapter": chapter,
                "title": title,
                "md": body,
                "slug": slugify(title),
                "chapter_slug": slugify(chapter),
            }
        )
        pid += 1
        buf = []

    for line in lines:
        if line.startswith("## "):
            flush()
            chapter = line[3:].strip()
            title = chapter
            continue
        if line.startswith("### "):
            flush()
            title = line[4:].strip()
            continue
        if line.startswith("# ") or "阅读格式" in line:
            continue
        buf.append(line)
    flush()
    if pages and pages[0]["title"] == "封面":
        pages[0]["title"] = "开篇"
        pages[0]["chapter"] = "文档导航"
        pages[0]["slug"] = "开篇"
        pages[0]["chapter_slug"] = "文档导航"
    return pages


def build_link_maps(pages: list[dict]) -> tuple[dict[str, str], str]:
    """锚点/标题 → 页面 id；相关文档页 id（用于外链兜底）。"""
    anchor_to_id: dict[str, str] = {}
    related_id = pages[-1]["id"]
    for p in pages:
        for key in (
            p["id"],
            p["slug"],
            p["chapter_slug"],
            p["title"],
            p["chapter"],
            slugify(p["title"]),
            slugify(p["chapter"]),
        ):
            if key:
                anchor_to_id[key] = p["id"]
                anchor_to_id[urllib.parse.unquote(key)] = p["id"]
        if "相关文档" in p["title"] or p["title"] == "相关文档":
            related_id = p["id"]
        # 兼容旧版「1. 产品概述」式锚点
        for prefix in ("1-", "2-", "3-", "4-", "5-", "6-"):
            if p["chapter_slug"].startswith(prefix) or p["slug"].startswith(prefix):
                continue
        bare = re.sub(r"^\d+-?", "", p["chapter_slug"])
        if bare:
            anchor_to_id[bare] = p["id"]
            anchor_to_id[f"1-{bare}"] = p["id"]
            anchor_to_id[f"2-{bare}"] = p["id"]
            anchor_to_id[f"3-{bare}"] = p["id"]
            anchor_to_id[f"4-{bare}"] = p["id"]
            anchor_to_id[f"5-{bare}"] = p["id"]
            anchor_to_id[f"6-{bare}"] = p["id"]
    # 显式栏目映射（导航表常用）
    for name, aliases in {
        "产品概述": ["1-产品概述", "产品概述"],
        "快速入门": ["2-快速入门", "快速入门"],
        "操作指南": ["3-操作指南", "操作指南"],
        "实践教程": ["4-实践教程", "实践教程"],
        "安全合规": ["5-安全合规", "安全合规"],
        "服务支持": ["6-服务支持", "服务支持"],
        "文档导航": ["文档导航", "开篇"],
    }.items():
        for p in pages:
            if p["chapter"] == name or p["title"] == name:
                for a in aliases:
                    anchor_to_id[a] = p["id"]
                break
    return anchor_to_id, related_id


def embed_image(rel: str) -> str | None:
    path = (ASSETS.parent / rel).resolve() if not rel.startswith("assets/") else (ASSETS / Path(rel).name)
    if "assets/" in rel.replace("\\", "/"):
        path = ASSETS / Path(rel.replace("\\", "/").split("assets/")[-1])
    if not path.is_file():
        return None
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    suffix = path.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}.get(
        suffix, "image/png"
    )
    return f"data:{mime};base64,{data}"


def resolve_href(href: str, anchor_to_id: dict[str, str], related_id: str) -> str:
    href = href.strip()
    if href.startswith("#"):
        key = urllib.parse.unquote(href[1:])
        pid = anchor_to_id.get(key) or anchor_to_id.get(slugify(key))
        if pid:
            return f"#{pid}"
        # 模糊：标题包含
        for k, v in anchor_to_id.items():
            if key in k or k in key:
                return f"#{v}"
        return f"#{related_id}"
    low = href.lower()
    if low.endswith(".html") and "用户使用说明书" in href:
        return "#p0"
    if low.endswith((".md", ".docx", ".doc")) or "../" in href or href.startswith("docs/"):
        return f"#{related_id}"
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return f"#{related_id}"


def make_inline(anchor_to_id: dict[str, str], related_id: str):
    def inline(text: str) -> str:
        text = html.escape(text)

        def repl_link(m: re.Match[str]) -> str:
            label, href = m.group(1), html.unescape(m.group(2))
            if href.startswith("assets/") or re.search(r"\.(png|jpe?g|gif|webp)$", href, re.I):
                b64 = embed_image(href)
                if b64:
                    return f'<img class="fig" src="{b64}" alt="{label}">'
                return f'<span class="missing-fig">【插图暂缺：{label}】</span>'
            resolved = resolve_href(href, anchor_to_id, related_id)
            return f'<a href="{html.escape(resolved)}">{label}</a>'

        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl_link, text)
        return text

    return inline


def md_block(md: str, inline) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            out.append("<hr>")
            i += 1
            continue
        # 独立图片行 ![alt](path)
        m_img = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", line.strip())
        if m_img:
            alt, src = m_img.group(1), m_img.group(2)
            b64 = embed_image(src)
            cap = html.escape(alt) if alt else ""
            if b64:
                out.append(
                    f'<figure class="shot"><img src="{b64}" alt="{cap}">'
                    + (f"<figcaption>{cap}</figcaption>" if cap else "")
                    + "</figure>"
                )
            else:
                out.append(f'<p class="missing-fig">【插图暂缺：{html.escape(alt or src)}】</p>')
            i += 1
            continue
        if line.startswith("```"):
            fence = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                fence.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(fence) + "</code></pre>")
            continue
        if line.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].startswith("> "):
                quote.append(inline(lines[i][2:]))
                i += 1
            out.append('<blockquote class="callout">' + "<br>".join(quote) + "</blockquote>")
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                raw = [c.strip() for c in lines[i].strip("|").split("|")]
                rows.append(raw)
                i += 1
            if len(rows) >= 2 and all(set(c) <= set("-: ") for c in rows[1]):
                header, body = rows[0], rows[2:]
            else:
                header, body = rows[0], rows[1:]
            thead = "".join(f"<th>{inline(c)}</th>" for c in header)
            tbody = ""
            for r in body:
                tbody += "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
            out.append(
                f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>'
            )
            continue
        if re.match(r"^\d+\.\s", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\d+\.\s", "", lines[i])) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append("<li>" + inline(lines[i][2:]) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if not line.strip():
            i += 1
            continue
        out.append("<p>" + inline(line) + "</p>")
        i += 1
    return "\n".join(out)


def grams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text.lower())
    g = set()
    for n in (2, 3):
        for i in range(max(0, len(t) - n + 1)):
            g.add(t[i : i + n])
    for w in re.findall(r"[a-z0-9_]{2,}", text.lower()):
        g.add(w)
    return g


INDEX_TERMS = [
    ("T+1", ["当日买入", "次日卖出", "模拟盘"]),
    ("100 股 / 1 手", ["手数", "买不起"]),
    ("15% 单票上限", ["减仓", "集中度"]),
    ("8% 止盈止损", ["参考止损价", "三重障碍"]),
    ("明日待办", ["最多 3 条", "执行日"]),
    ("观望", ["一等动作", "买不起 1 手"]),
    ("建仓", ["前 50", "以损定仓"]),
    ("置信度", ["分位", "排名"]),
    ("数据未就绪", ["涨跌停价", "市值基本面"]),
    ("模拟盘", ["一万练习账户", "滑点"]),
    ("冠军模型", ["模型编号"]),
    ("已关联成交", ["确认成交"]),
    ("失效条件", ["涨停不可买", "可核对"]),
    ("AI 解释", ["通义", "不产生建议"]),
    ("掘金扫描", ["排序", "观察名单"]),
    ("不下单", ["同花顺", "券商"]),
]


def build() -> str:
    pages = parse_pages(SRC.read_text(encoding="utf-8"))
    anchor_to_id, related_id = build_link_maps(pages)
    inline = make_inline(anchor_to_id, related_id)
    for p in pages:
        p["html"] = md_block(p["md"], inline)
        p["text"] = re.sub(r"<[^>]+>", " ", p["html"])
        p["search"] = " ".join([p["chapter"], p["title"], p["text"]])

    toc_chapters: list[dict] = []
    for p in pages:
        if not toc_chapters or toc_chapters[-1]["name"] != p["chapter"]:
            toc_chapters.append({"name": p["chapter"], "items": []})
        toc_chapters[-1]["items"].append({"id": p["id"], "title": p["title"]})

    index_rows = []
    for term, aliases in INDEX_TERMS:
        hits = []
        blob = term + " " + " ".join(aliases)
        keys = grams(blob)
        for p in pages:
            pg = grams(p["search"])
            if keys & pg or term in p["search"]:
                hits.append({"id": p["id"], "title": p["title"]})
        index_rows.append({"term": term, "aliases": aliases, "hits": hits[:8]})

    payload = {
        "pages": [
            {
                "id": p["id"],
                "chapter": p["chapter"],
                "title": p["title"],
                "html": p["html"],
            }
            for p in pages
        ],
        "toc": toc_chapters,
        "index": index_rows,
        "relatedId": related_id,
    }

    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return TEMPLATE.replace("/*__DATA__*/", data_json)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>研衡 · 用户使用说明书</title>
<style>
:root {
  --ink: #1a2332;
  --muted: #5c6b7a;
  --paper: #f6f1e8;
  --card: #fffdf8;
  --navy: #173a63;
  --navy-2: #0f2744;
  --line: #d9cfc0;
  --accent: #b45309;
  --shadow: 0 18px 50px rgba(23, 58, 99, .12);
  --sidebar: 300px;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: "Source Han Serif SC", "Noto Serif SC", "Songti SC", "SimSun", Georgia, serif;
  color: var(--ink);
  background:
    radial-gradient(1200px 500px at 10% -10%, rgba(23,58,99,.08), transparent 50%),
    var(--paper);
}
code, pre, kbd, .mono {
  font-family: "Cascadia Mono", "Sarasa Mono SC", Consolas, monospace;
}
.app {
  display: grid;
  grid-template-columns: var(--sidebar) 1fr;
  min-height: 100%;
}
.side {
  background: linear-gradient(180deg, var(--navy-2), var(--navy));
  color: #e8eef5;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  position: sticky;
  top: 0;
  max-height: 100vh;
}
.brand {
  padding: 22px 20px 12px;
  border-bottom: 1px solid rgba(255,255,255,.08);
}
.brand .kicker {
  font-family: "Cascadia Mono", Consolas, monospace;
  font-size: 11px;
  letter-spacing: .18em;
  color: #c4a574;
  text-transform: uppercase;
}
.brand h1 {
  font-size: 20px;
  margin: 8px 0 4px;
  font-weight: 600;
}
.brand p { margin: 0; color: #b7c4d3; font-size: 12px; font-family: "Microsoft YaHei", sans-serif; }
.tabs {
  display: flex;
  gap: 0;
  padding: 0 8px;
  border-bottom: 1px solid rgba(255,255,255,.08);
}
.tabs button {
  flex: 1;
  background: none;
  border: 0;
  color: #9db0c3;
  padding: 10px 6px;
  cursor: pointer;
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 13px;
  border-bottom: 2px solid transparent;
}
.tabs button.on { color: #fff; border-bottom-color: #c4a574; }
.search-wrap { padding: 12px 14px 8px; }
.search-wrap input {
  width: 100%;
  border: 1px solid rgba(255,255,255,.15);
  background: rgba(0,0,0,.2);
  color: #fff;
  border-radius: 8px;
  padding: 8px 10px;
  font-family: "Microsoft YaHei", sans-serif;
}
.search-wrap input::placeholder { color: #8ea0b3; }
.nav {
  overflow: auto;
  padding: 8px 0 24px;
  flex: 1;
}
.chap {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 11px;
  letter-spacing: .08em;
  color: #c4a574;
  padding: 12px 16px 4px;
}
.nav a {
  display: block;
  color: #d5deea;
  text-decoration: none;
  padding: 7px 16px 7px 20px;
  font-size: 13px;
  font-family: "Microsoft YaHei", sans-serif;
  line-height: 1.4;
  border-left: 3px solid transparent;
}
.nav a:hover { background: rgba(255,255,255,.06); }
.nav a.active {
  background: rgba(196,165,116,.15);
  border-left-color: #c4a574;
  color: #fff;
}
.hit { padding: 8px 16px; cursor: pointer; }
.hit:hover { background: rgba(255,255,255,.06); }
.hit b { color: #fff; font-family: "Microsoft YaHei", sans-serif; font-size: 13px; }
.hit span { display: block; color: #9db0c3; font-size: 12px; font-family: "Microsoft YaHei", sans-serif; margin-top: 4px; }
.idx-term {
  padding: 10px 16px;
  border-bottom: 1px solid rgba(255,255,255,.06);
  font-family: "Microsoft YaHei", sans-serif;
}
.idx-term strong { color: #fff; font-size: 13px; }
.idx-term .als { color: #9db0c3; font-size: 11px; margin: 2px 0 6px; }
.idx-term button {
  background: rgba(255,255,255,.08);
  border: 0;
  color: #e8eef5;
  margin: 0 6px 6px 0;
  padding: 3px 8px;
  border-radius: 99px;
  cursor: pointer;
  font-size: 11px;
}
.stage { display: flex; flex-direction: column; min-height: 100vh; }
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 28px;
  border-bottom: 1px solid var(--line);
  background: rgba(255,253,248,.86);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 2;
}
.crumb { font-family: "Microsoft YaHei", sans-serif; font-size: 13px; color: var(--muted); }
.pager { display: flex; align-items: center; gap: 8px; }
.pager button, .hint kbd {
  font-family: "Microsoft YaHei", sans-serif;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--navy);
  border-radius: 8px;
  padding: 7px 12px;
  cursor: pointer;
}
.pager button:disabled { opacity: .4; cursor: default; }
.pager .num { font-variant-numeric: tabular-nums; color: var(--muted); min-width: 72px; text-align: center; }
.book {
  flex: 1;
  padding: 28px 8vw 80px;
  perspective: 1600px;
}
.sheet {
  max-width: 860px;
  margin: 0 auto;
  background: var(--card);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
  border-radius: 4px 16px 16px 4px;
  padding: 40px 48px 48px;
  min-height: 70vh;
  transform-origin: left center;
  animation: flipIn .38s ease;
}
@keyframes flipIn {
  from { opacity: 0; transform: rotateY(-9deg) translateX(18px); }
  to { opacity: 1; transform: none; }
}
.sheet .chap-label {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 12px;
  letter-spacing: .16em;
  color: var(--accent);
  text-transform: uppercase;
}
.sheet h2 {
  font-size: 28px;
  margin: 8px 0 20px;
  font-weight: 600;
  line-height: 1.3;
}
.sheet p, .sheet li { font-family: "Microsoft YaHei", "Noto Sans SC", sans-serif; line-height: 1.75; font-size: 15px; }
.sheet ul, .sheet ol { padding-left: 1.2em; }
.sheet blockquote.callout {
  margin: 16px 0;
  padding: 12px 16px;
  background: #fff6e8;
  border-left: 4px solid var(--accent);
  font-family: "Microsoft YaHei", sans-serif;
}
.table-wrap { overflow-x: auto; margin: 16px 0; }
table { border-collapse: collapse; width: 100%; font-family: "Microsoft YaHei", sans-serif; font-size: 13.5px; }
th, td { border: 1px solid var(--line); padding: 8px 10px; vertical-align: top; }
th { background: #173a63; color: #fff; font-weight: 600; }
tr:nth-child(even) td { background: #faf6ef; }
pre {
  background: #0f2744;
  color: #e8eef5;
  padding: 14px 16px;
  overflow-x: auto;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.5;
}
code { background: #efe7d8; padding: 1px 5px; border-radius: 4px; font-size: .92em; }
pre code { background: none; color: inherit; padding: 0; }
figure.shot {
  margin: 18px 0;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}
figure.shot img, img.fig {
  display: block;
  width: 100%;
  height: auto;
}
figure.shot figcaption {
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 12px;
  color: var(--muted);
  padding: 8px 12px;
  border-top: 1px solid var(--line);
  background: #faf6ef;
}
.missing-fig {
  font-family: "Microsoft YaHei", sans-serif;
  color: var(--accent);
  background: #fff6e8;
  padding: 10px 12px;
  border-radius: 6px;
}
.hint {
  position: sticky;
  bottom: 0;
  display: flex;
  justify-content: space-between;
  padding: 8px 28px 12px;
  color: var(--muted);
  font-family: "Microsoft YaHei", sans-serif;
  font-size: 12px;
  background: linear-gradient(transparent, var(--paper));
}
.hint kbd { padding: 2px 6px; font-size: 11px; }
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  .side { position: relative; max-height: none; }
  .book { padding: 16px 12px 80px; }
  .sheet { padding: 24px 18px; }
}
.mark { background: #ffe08a; padding: 0 2px; }
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <div class="kicker">YANHENG DOCS</div>
      <h1>用户使用说明书</h1>
      <p>v2.2 · 可翻页 · 可检索 · 可索引</p>
    </div>
    <div class="tabs">
      <button data-tab="toc" class="on">目录</button>
      <button data-tab="search">检索</button>
      <button data-tab="index">索引</button>
    </div>
    <div class="search-wrap" id="searchBox" hidden>
      <input id="q" type="search" placeholder="输入关键词，如 待办、阻塞、止损…" autocomplete="off">
    </div>
    <nav class="nav" id="nav"></nav>
  </aside>
  <main class="stage">
    <div class="toolbar">
      <div class="crumb" id="crumb"></div>
      <div class="pager">
        <button id="prev" type="button">上一页</button>
        <div class="num" id="num"></div>
        <button id="next" type="button">下一页</button>
      </div>
    </div>
    <div class="book">
      <article class="sheet" id="sheet"></article>
    </div>
    <div class="hint">
      <span>键盘 ← → 翻页 · / 聚焦检索 · Esc 清空</span>
      <span>全部输出仅供研究辅助，不构成投资建议</span>
    </div>
  </main>
</div>
<script>
const DATA = /*__DATA__*/;
let i = 0;
let tab = "toc";

function grams(s) {
  const t = String(s).toLowerCase().replace(/\s+/g, "");
  const g = new Set();
  for (const n of [2, 3]) {
    for (let k = 0; k <= t.length - n; k++) g.add(t.slice(k, k + n));
  }
  (String(s).toLowerCase().match(/[a-z0-9_]{2,}/g) || []).forEach(w => g.add(w));
  return g;
}
const INDEXED = DATA.pages.map(p => ({
  id: p.id,
  title: p.title,
  chapter: p.chapter,
  text: p.html.replace(/<[^>]+>/g, " "),
  grams: grams(p.chapter + p.title + p.html.replace(/<[^>]+>/g, " "))
}));

function highlight(html, q) {
  if (!q || q.length < 2) return html;
  const esc = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  try {
    return html.replace(new RegExp(esc, "gi"), m => `<span class="mark">${m}</span>`);
  } catch (e) { return html; }
}

function renderSheet() {
  const p = DATA.pages[i];
  const q = (document.getElementById("q").value || "").trim();
  const sheet = document.getElementById("sheet");
  sheet.style.animation = "none";
  void sheet.offsetWidth;
  sheet.style.animation = "";
  sheet.innerHTML = `<div class="chap-label">${p.chapter}</div><h2>${p.title}</h2>${highlight(p.html, q)}`;
  document.getElementById("crumb").textContent = p.chapter + "  /  " + p.title;
  document.getElementById("num").textContent = (i + 1) + " / " + DATA.pages.length;
  document.getElementById("prev").disabled = i === 0;
  document.getElementById("next").disabled = i === DATA.pages.length - 1;
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.i == i));
  location.hash = p.id;
  sheet.querySelectorAll("a[href^='#']").forEach(a => {
    a.addEventListener("click", ev => {
      const id = a.getAttribute("href").slice(1);
      const idx = DATA.pages.findIndex(x => x.id === id);
      if (idx >= 0) {
        ev.preventDefault();
        goto(idx);
      }
    });
  });
}

function goto(n) {
  i = Math.max(0, Math.min(DATA.pages.length - 1, n));
  renderSheet();
  window.scrollTo(0, 0);
}

function renderToc() {
  const nav = document.getElementById("nav");
  nav.innerHTML = DATA.toc.map(ch => {
    const items = ch.items.map(it => {
      const idx = DATA.pages.findIndex(p => p.id === it.id);
      return `<a href="#${it.id}" data-i="${idx}">${it.title}</a>`;
    }).join("");
    return `<div class="chap">${ch.name}</div>${items}`;
  }).join("");
}

function search(q) {
  q = q.trim();
  const nav = document.getElementById("nav");
  if (q.length < 1) { renderToc(); return; }
  const gq = grams(q);
  const scored = INDEXED.map((p, idx) => {
    let s = 0;
    if (p.title.includes(q) || p.chapter.includes(q)) s += 50;
    if (p.text.includes(q)) s += 20;
    gq.forEach(g => { if (p.grams.has(g)) s += 1; });
    return { idx, p, s };
  }).filter(x => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 24);
  if (!scored.length) {
    nav.innerHTML = `<div class="hit"><span>没有匹配「${q}」的条目</span></div>`;
    return;
  }
  nav.innerHTML = scored.map(({idx, p}) => {
    const pos = p.text.indexOf(q);
    const snip = pos >= 0 ? p.text.slice(Math.max(0, pos - 18), pos + q.length + 28) : p.text.slice(0, 48);
    return `<div class="hit" data-i="${idx}"><b>${p.title}</b><span>${p.chapter} · …${snip}…</span></div>`;
  }).join("");
}

function renderIndex() {
  const nav = document.getElementById("nav");
  nav.innerHTML = DATA.index.map(row => {
    const btns = row.hits.map(h => {
      const idx = DATA.pages.findIndex(p => p.id === h.id);
      return `<button type="button" data-i="${idx}">${h.title}</button>`;
    }).join("");
    return `<div class="idx-term"><strong>${row.term}</strong><div class="als">${row.aliases.join(" · ")}</div>${btns || "<span class='als'>无命中</span>"}</div>`;
  }).join("");
}

function setTab(name) {
  tab = name;
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  document.getElementById("searchBox").hidden = name !== "search";
  if (name === "toc") renderToc();
  else if (name === "search") { renderToc(); document.getElementById("q").focus(); search(document.getElementById("q").value); }
  else renderIndex();
}

document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));
document.getElementById("q").addEventListener("input", e => search(e.target.value));
document.getElementById("prev").onclick = () => goto(i - 1);
document.getElementById("next").onclick = () => goto(i + 1);
document.getElementById("nav").addEventListener("click", e => {
  const t = e.target.closest("[data-i]");
  if (!t) return;
  e.preventDefault();
  goto(Number(t.dataset.i));
});
document.addEventListener("keydown", e => {
  if (e.key === "ArrowLeft") goto(i - 1);
  if (e.key === "ArrowRight") goto(i + 1);
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") {
    e.preventDefault();
    setTab("search");
  }
  if (e.key === "Escape") {
    document.getElementById("q").value = "";
    if (tab === "search") search("");
  }
});
window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  const idx = DATA.pages.findIndex(p => p.id === id);
  if (idx >= 0 && idx !== i) goto(idx);
});

renderToc();
const start = DATA.pages.findIndex(p => p.id === location.hash.slice(1));
goto(start >= 0 ? start : 0);
</script>
</body>
</html>
"""


def main() -> None:
    html_doc = build()
    OUT.write_text(html_doc, encoding="utf-8")
    pages = parse_pages(SRC.read_text(encoding="utf-8"))
    print(OUT, "pages=", len(pages))


if __name__ == "__main__":
    main()
