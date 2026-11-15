#!/usr/bin/env python3
"""Keep nathanpmyoung.com the Logseq app, and add a plain HTML copy of every public note for crawlers.

python3 sitegen/build.py --spa spa --out www   # spa = what logseq/publish-spa built
Crawlers that don't run JavaScript see nothing in the Logseq app. Only reads the graph this
script sits in; sitegen/gate.py checks the output.
"""
import argparse, datetime as dt, html, os, re, shutil, subprocess, sys, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = "https://nathanpmyoung.com"
SUBSTACK = "https://nathanpmyoung.substack.com"
NAME = "Nathan Young"
HIDDEN_PROPS = {"id", "collapsed", "heading", "background-color", "title", "alias", "aliases", "public", "filters"}
e = html.escape


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "page"


# ---------- Logseq graph ----------

def ordinal(d):
    return f"{d}{'th' if 11 <= d % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d % 10, 'th')}"


def graph_files():
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-s", "-z", "--", "pages", "journals", "*.md"],
                         capture_output=True, text=True, check=True).stdout
    files = (entry.split("\t", 1) for entry in out.split("\0") if entry)
    return sorted({p for meta, p in files if not meta.startswith("120000") and p.endswith(".md")
                   and (p.startswith(("pages/", "journals/")) or "/" not in p)})


def git_dates():
    out = subprocess.run(["git", "-C", ROOT, "log", "--format=@%cI", "--name-only"], capture_output=True, text=True).stdout
    dates, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@"):
            cur = line[1:]
        elif line and line not in dates:
            dates[line] = cur
    return dates


def parse_blocks(text):
    """Logseq outline -> (page props, [block]) where block = {text, props, children}."""
    root = {"children": [], "lines": []}
    stack = [(-1, root)]
    loose = []  # lines before the first bullet
    for raw in text.replace("\r\n", "\n").split("\n"):
        stripped = raw.lstrip("\t")
        depth = len(raw) - len(stripped)
        lead = len(stripped) - len(stripped.lstrip(" "))
        if lead and not depth:  # space-indented outline
            depth, stripped = lead // 2, stripped.lstrip(" ")
        if stripped.startswith("- ") or stripped == "-":
            blk = {"lines": [stripped[2:]], "children": []}
            while stack[-1][0] >= depth:
                stack.pop()
            stack[-1][1]["children"].append(blk)
            stack.append((depth, blk))
        elif len(stack) > 1:
            stack[-1][1]["lines"].append(stripped[2:] if stripped.startswith("  ") else stripped)
        else:
            loose.append(raw)

    def finish(b):
        props, body, fence = {}, [], False
        for ln in b["lines"]:
            if ln.strip().startswith("```"):
                fence = not fence
            m = None if fence else re.match(r"^\s*([A-Za-z0-9_.-]+):: ?(.*)$", ln)
            if m:
                props[m.group(1).lower()] = m.group(2).strip()
            else:
                body.append(ln)
        b["props"], b["text"] = props, "\n".join(body).strip()
        for c in b["children"]:
            finish(c)

    for c in root["children"]:
        finish(c)
    page_props = {}
    for ln in loose:
        m = re.match(r"^([A-Za-z0-9_.-]+):: ?(.*)$", ln.strip())
        if m:
            page_props[m.group(1).lower()] = m.group(2).strip()
    blocks = root["children"]
    if blocks and not blocks[0]["text"] and blocks[0]["props"] and not blocks[0]["children"]:
        page_props.update(blocks[0]["props"])
        blocks = blocks[1:]
    prose = "\n".join(l for l in loose if not re.match(r"^\s*[A-Za-z0-9_.-]+:: ", l)).strip()
    return page_props, blocks, prose


def load_graph():
    pages, dates = [], git_dates()
    for path in graph_files():
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            props, blocks, prose = parse_blocks(f.read())
        if props.get("public", "").lower() == "false":
            continue
        stem = urllib.parse.unquote(os.path.basename(path)[:-3])
        journal = None
        if path.startswith("journals/"):
            try:
                journal = dt.datetime.strptime(stem, "%Y_%m_%d").date()
            except ValueError:
                pass
        if journal:
            title = f"{journal.strftime('%b')} {ordinal(journal.day)}, {journal.year}"
            url = f"journal/{journal.isoformat()}/"
        else:
            title = props.get("title") or stem.replace("___", "/")
            url = None
        aliases = [a.strip().strip("[]") for a in re.split(r",", props.get("alias", "") + "," + props.get("aliases", "")) if a.strip()]
        pages.append(dict(title=title, url=url, source=path, props=props, blocks=blocks, prose=prose,
                          journal=journal, aliases=aliases, updated=dates.get(path), kind="note"))
    return pages


# ---------- the site ----------

class Site:
    def __init__(self, notes):
        self.pages, self.by_name, self.backlinks, self.assets, self.blocks_by_id = [], {}, {}, set(), {}
        used = set()

        def claim(base):
            u, n = base, 2
            while u in used:
                u, n = f"{base[:-1]}-{n}/", n + 1
            used.add(u)
            return u

        for n in notes:
            n["url"] = claim(n["url"] or f"notes/{slugify(n['title'])}/")
            self.add(n)
            self.index_blocks(n["blocks"], n)
        self.home = self.by_name.get(NAME.lower())
        # Pass 1 counts links so link-only names (tags) with 2+ referrers get a stub page, as Logseq would.
        self.cur, self.counting = None, {}
        for n in notes:
            self.cur = n
            self.walk_text(n)
        for name, (label, refs) in sorted(self.counting.items()):
            if len(refs) >= 2 and name not in self.by_name:
                self.add(dict(kind="stub", title=label, url=claim(f"notes/{slugify(label)}/"), blocks=[], prose="",
                              aliases=[], journal=None, source=None, updated=None, props={}))
        self.counting = None

    def add(self, p):
        self.pages.append(p)
        for name in [p["title"]] + p["aliases"]:
            self.by_name.setdefault(name.lower(), p)

    def index_blocks(self, blocks, page):
        for b in blocks:
            if b["props"].get("id"):
                self.blocks_by_id[b["props"]["id"]] = (b, page)
            self.index_blocks(b["children"], page)

    def walk_text(self, n):
        def go(bs):
            for b in bs:
                self.inline(b["text"], "")
                go(b["children"])
        go(n["blocks"])
        self.inline(n["prose"], "")

    # Inline markdown -> HTML. `rel` is the path back to the site root from the page being written.
    TOKEN = re.compile(
        r"(?P<iframe><iframe[^>]*?\bsrc=\"(?P<fsrc>https?://[^\"]+)\"[^>]*>\s*(?:</iframe>)?)"
        r"|(?P<code>`[^`\n]+`)"
        r"|(?P<macro>\{\{(?P<mname>[a-z-]+)\s*(?P<marg>.*?)\}\})"
        r"|(?P<bref>\(\((?P<bid>[0-9a-f-]{36})\)\))"
        r"|(?P<img>!\[(?P<ialt>[^\]]*)\]\((?P<isrc>[^)\s]+)\))"
        r"|(?P<mdlink>\[(?P<ltext>[^\]\n]+)\]\((?P<ltarget>(?:\[\[[^\]]+\]\]|\{\{[^}]*\}\}|[^()\s]|\([^()\s]*\)|\s(?=https?:))+)\))"
        r"|(?P<tagw>#\[\[(?P<tag2>[^\]]+)\]\])"
        r"|(?P<wiki>\[\[(?P<wname>[^\]\[]+)\]\])"
        r"|(?P<tag>(?<![\w/&#])#(?P<tname>[A-Za-z][\w/-]*))"
        r"|(?P<bold>\*\*(?P<btext>.+?)\*\*)"
        r"|(?P<ital>(?<![\w*])[*_](?P<itext>[^*_\s][^*_\n]*?)[*_](?![\w*]))"
        r"|(?P<strike>~~(?P<stext>.+?)~~)"
        r"|(?P<hl>==(?P<htext>.+?)==)"
        r"|(?P<url>https?://[^\s<>\]\)]+[^\s<>\]\).,;:!?'\"])"
    )

    def page_link(self, name, rel, label=None, cls=""):
        name = name.strip()
        if not name:
            return ""
        target = self.by_name.get(name.lower())
        if self.counting is not None and self.cur:
            self.counting.setdefault(name.lower(), (name, set()))[1].add(self.cur["url"])
        if target and self.cur is not None and target is not self.cur and self.counting is None:
            self.backlinks.setdefault(target["url"], {}).setdefault(self.cur["url"], (self.cur, []))
        text = e(label if label is not None else name)
        if not target:
            return f'<span class="ghost">{text}</span>'
        return f'<a class="{cls or "ref"}" href="{rel}{target["url"]}">{text}</a>'

    def ext_link(self, url, rel, label):
        if not re.match(r"^(https?:|mailto:)", url):
            return label
        return f'<a href="{e(url)}">{label}</a>'

    def inline(self, text, rel):
        out, pos = [], 0
        for m in self.TOKEN.finditer(text):
            out.append(e(text[pos:m.start()], quote=False))
            pos = m.end()
            g = m.lastgroup
            if m.group("iframe"):
                out.append(f'<a class="media" href="{e(m.group("fsrc"))}">embed: {e(m.group("fsrc"))}</a>')
            elif m.group("code"):
                out.append(f"<code>{e(m.group('code')[1:-1])}</code>")
            elif m.group("macro"):
                out.append(self.macro(m.group("mname"), m.group("marg").strip(), rel))
            elif m.group("bref"):
                hit = self.blocks_by_id.get(m.group("bid"))
                out.append(f'<span class="bref">{self.inline(hit[0]["text"], rel)}</span>' if hit else "")
            elif m.group("img"):
                out.append(self.image(m.group("isrc"), m.group("ialt"), rel))
            elif m.group("mdlink"):
                label, target = self.inline(m.group("ltext"), rel), m.group("ltarget").strip()
                wm = re.fullmatch(r"\[\[(.+)\]\]", target)
                if wm:
                    out.append(self.page_link_html(wm.group(1), rel, label))
                else:
                    urls = re.findall(r"https?://[^\s}]+", target) or [target]
                    out.append(self.ext_link(urls[-1], rel, label))
            elif m.group("tagw"):
                out.append(self.page_link(m.group("tag2"), rel, "#" + m.group("tag2"), "tag"))
            elif m.group("wiki"):
                out.append(self.page_link(m.group("wname"), rel))
            elif m.group("tag"):
                out.append(self.page_link(m.group("tname"), rel, "#" + m.group("tname"), "tag"))
            elif m.group("bold"):
                out.append(f"<strong>{self.inline(m.group('btext'), rel)}</strong>")
            elif m.group("ital"):
                out.append(f"<em>{self.inline(m.group('itext'), rel)}</em>")
            elif m.group("strike"):
                out.append(f"<s>{self.inline(m.group('stext'), rel)}</s>")
            elif m.group("hl"):
                out.append(f"<mark>{self.inline(m.group('htext'), rel)}</mark>")
            elif g == "url":
                out.append(self.ext_link(m.group("url"), rel, e(m.group("url"))))
        out.append(e(text[pos:], quote=False))
        return "".join(out)

    def page_link_html(self, name, rel, label_html):
        a = self.page_link(name, rel, "", "ref")
        return re.sub(r">[^<]*</(a|span)>$", lambda m: f">{label_html}</{m.group(1)}>", a)

    def macro(self, name, arg, rel):
        if name == "embed":
            wm = re.fullmatch(r"\[\[(.+)\]\]", arg)
            if wm:
                return f'<span class="embed">{self.page_link(wm.group(1), rel)}</span>'
            bm = re.fullmatch(r"\(\(([0-9a-f-]{36})\)\)", arg)
            hit = bm and self.blocks_by_id.get(bm.group(1))
            if hit:
                return f'<div class="embed">{self.render_blocks([hit[0]], rel)}</div>'
            return ""
        if name in ("video", "youtube", "tweet", "twitter", "vimeo", "pdf") and re.match(r"https?://", arg):
            return f'<a class="media" href="{e(arg)}">{e(name)}: {e(arg)}</a>'
        return ""

    def image(self, src, alt, rel):
        if src.startswith("../assets/"):
            name = os.path.basename(urllib.parse.unquote(src))
            if os.path.isfile(os.path.join(ROOT, "assets", name)):
                self.assets.add(name)
                return f'<img src="{rel}assets/{e(urllib.parse.quote(name))}" alt="{e(alt)}" loading="lazy">'
            return ""
        if src.startswith("https://"):
            return f'<img src="{e(src)}" alt="{e(alt)}" loading="lazy">'
        return ""

    def block_html(self, b, rel):
        t = b["text"]
        out, para, fence = [], [], None
        lines = t.split("\n")
        marker = re.match(r"^(TODO|DONE|LATER|NOW|DOING|WAITING|CANCELED|CANCELLED) ", t)
        if marker:
            lines[0] = lines[0][len(marker.group(0)):]

        def flush():
            if para:
                out.append(self.inline("\n".join(para), rel).replace("\n", "<br>"))
                para.clear()

        for ln in lines:
            if fence is not None:
                if ln.strip().startswith("```"):
                    out.append(f"<pre><code>{e(chr(10).join(fence))}</code></pre>")
                    fence = None
                else:
                    fence.append(ln)
                continue
            if ln.strip().startswith("```"):
                flush()
                fence = []
                continue
            h = re.match(r"^(#{1,6}) (.*)", ln)
            if h:
                flush()
                lvl = min(len(h.group(1)) + 1, 6)
                out.append(f"<h{lvl}>{self.inline(h.group(2), rel)}</h{lvl}>")
                continue
            if ln.startswith("> "):
                flush()
                out.append(f"<blockquote>{self.inline(ln[2:], rel)}</blockquote>")
                continue
            para.append(ln)
        flush()
        if fence:
            out.append(f"<pre><code>{e(chr(10).join(fence))}</code></pre>")
        props = "".join(f'<div class="prop"><span>{e(k)}</span> {self.inline(v, rel)}</div>'
                        for k, v in b["props"].items() if k not in HIDDEN_PROPS)
        pre = f'<span class="marker {marker.group(1).lower()}">{marker.group(1)}</span> ' if marker else ""
        return pre + "".join(out) + props

    def render_blocks(self, blocks, rel):
        items = []
        for b in blocks:
            body = self.block_html(b, rel)
            if not body.strip() and not b["children"]:
                continue
            if self.cur is not None and self.counting is None:
                # Record which block each backlink came from, for the "Linked references" section.
                for url, srcs in self.backlinks.items():
                    ent = srcs.get(self.cur["url"])
                    if ent and (not ent[1] or ent[1][-1][0] is not b) and f'href="{rel}{url}' in body:
                        ent[1].append((b, body))
            kids = self.render_blocks(b["children"], rel) if b["children"] else ""
            items.append(f"<li><div class=\"b\">{body}</div>{kids}</li>")
        return f'<ul class="blocks">{"".join(items)}</ul>' if items else ""

    def prose_html(self, text, rel):
        out = []
        for chunk in re.split(r"\n\s*\n", text.strip()):
            lines = chunk.split("\n")
            if all(re.match(r"^\s*\|", l) for l in lines) and len(lines) >= 2:
                rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines if not re.match(r"^\s*\|[\s:|-]+\|\s*$", l)]
                cells = lambda r, t: "".join(f"<{t}>{self.inline(c, rel)}</{t}>" for c in r)
                out.append("<table><tr>" + cells(rows[0], "th") + "</tr>" + "".join(f"<tr>{cells(r, 'td')}</tr>" for r in rows[1:]) + "</table>")
                continue
            h = re.match(r"^(#{1,6}) (.*)$", chunk)
            if h and len(lines) == 1:
                lvl = min(len(h.group(1)) + 1, 6)
                out.append(f"<h{lvl}>{self.inline(h.group(2), rel)}</h{lvl}>")
            elif all(re.match(r"^\s*[-*] ", l) for l in lines):
                out.append("<ul>" + "".join(f"<li>{self.inline(re.sub(r'^\s*[-*] ', '', l), rel)}</li>" for l in lines) + "</ul>")
            else:
                out.append(f"<p>{self.inline(chunk, rel).replace(chr(10), '<br>')}</p>")
        return "".join(out)


CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--mute:#6a6f76;--line:#e4e1da;--link:#155e75;--ref:#7c3aed;--card:#f3f1ec}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e6e3;--mute:#9a9ea5;--line:#2c3036;--link:#67c3d9;--ref:#b9a3ff;--card:#1e2126}}
*{box-sizing:border-box}html{background:var(--bg);color:var(--fg)}
body{margin:0 auto;max-width:46rem;padding:1.2rem 1.1rem 4rem;font:17px/1.6 Charter,"Iowan Old Style",Georgia,serif}
header.site{display:flex;flex-wrap:wrap;gap:.4rem 1.1rem;align-items:baseline;border-bottom:1px solid var(--line);padding-bottom:.6rem;margin-bottom:1.6rem;font-family:system-ui,sans-serif;font-size:.92rem}
header.site .me{font-weight:700;font-size:1.05rem;color:var(--fg);text-decoration:none;margin-right:auto}
header.site a{color:var(--mute);text-decoration:none}header.site a:hover{color:var(--fg)}
a{color:var(--link)}a.ref{color:var(--ref);text-decoration:none;border-bottom:1px dotted}a.tag{color:var(--mute);text-decoration:none;font-size:.92em}
.ghost{border-bottom:1px dotted var(--line)}
h1{font-size:1.9rem;line-height:1.2;margin:.2rem 0 .3rem}.sub{color:var(--mute);margin:0 0 1rem;font-size:1.05rem}
.meta{color:var(--mute);font:.85rem system-ui,sans-serif;margin-bottom:1.4rem}
ul.blocks{list-style:none;padding-left:1.15rem;margin:0}ul.blocks ul.blocks{border-left:1px solid var(--line);margin-left:.25rem}
ul.blocks li{position:relative;margin:.15rem 0}ul.blocks li::before{content:"";position:absolute;left:-.82rem;top:.68em;width:.34rem;height:.34rem;border-radius:50%;background:var(--mute);opacity:.7}
.b h2,.b h3,.b h4{margin:.5rem 0 .1rem}.prop{font:.8rem system-ui,sans-serif;color:var(--mute)}.prop span{font-weight:600}
.marker{font:.72rem system-ui,sans-serif;padding:0 .3rem;border:1px solid var(--line);border-radius:3px;color:var(--mute)}
.embed{display:block;background:var(--card);border-radius:4px;padding:.2rem .5rem;margin:.2rem 0}
img{max-width:100%;height:auto}pre{overflow:auto;background:var(--card);padding:.6rem;border-radius:4px;font-size:.85rem}
code{font-size:.88em}blockquote{border-left:3px solid var(--line);margin:.5rem 0;padding-left:.9rem;color:var(--mute)}
table{border-collapse:collapse;font-size:.9rem;margin:1rem 0;display:block;overflow:auto}td,th{border:1px solid var(--line);padding:.25rem .5rem}
section.refs{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:1rem}
section.refs h2,h2.small{font:600 .8rem system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
.ref-src{margin:.9rem 0 .2rem;font-weight:600}.ref-snip{background:var(--card);border-radius:4px;padding:.3rem .7rem;margin:.2rem 0;font-size:.95rem}
.list{list-style:none;padding:0;margin:0 0 2rem}.list li{margin:.35rem 0;display:flex;gap:.8rem}.list time{color:var(--mute);font:.82rem/1.9 system-ui,sans-serif;white-space:nowrap;min-width:5.6rem}
.notes-grid{columns:15rem;column-gap:1.5rem;font-size:.95rem}.notes-grid a{display:block;break-inside:avoid;padding:.1rem 0}
footer{margin-top:3rem;color:var(--mute);font:.8rem system-ui,sans-serif}
"""


def shell(page_title, body, rel, url, desc="", noindex=False, extra_head="", logseq_title=None):
    canon = f"{SITE}/{url}"
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    live = f'<a href="{rel}#/page/{e(urllib.parse.quote(logseq_title.lower()))}">Open in Logseq view</a>' if logseq_title else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(page_title)}</title><meta name="description" content="{e(desc[:200])}">
<link rel="canonical" href="{e(canon)}">{robots}<meta name="author" content="{NAME}">
<meta property="og:title" content="{e(page_title)}"><meta property="og:description" content="{e(desc[:200])}"><meta property="og:url" content="{e(canon)}">
<link rel="stylesheet" href="{rel}notes.css">{extra_head}</head><body>
<header class="site"><a class="me" href="{rel}">{NAME}</a><a href="{rel}notes/">All notes</a><a href="{rel}journal/">Journal</a>{live}<a href="{SUBSTACK}">Blog</a></header>
<main>{body}</main>
<footer>A plain-text copy of a page from Nathan's public <a href="https://logseq.com">Logseq</a> graph, for readers and crawlers that don't run JavaScript.</footer>
</body></html>"""


def plain(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def build(out, spa, noindex=False):
    """Copy the Logseq app (publish-spa output) to `out` and add one plain HTML page per public note."""
    site = Site(load_graph())
    if not os.path.isfile(os.path.join(spa, "index.html")):
        sys.exit(f"{spa} has no index.html; expected the Logseq publish-spa output")
    if os.path.isdir(out):
        shutil.rmtree(out)
    shutil.copytree(spa, out, symlinks=False, ignore=shutil.ignore_patterns(".git"))

    def write(rel_path, text):
        path = os.path.join(out, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    bodies = {}
    # Render every body first so backlinks are complete before any page is written.
    for p in site.pages:
        site.cur = p
        rel = "../" * p["url"].count("/")
        bodies[p["url"]] = site.render_blocks(p["blocks"], rel) + (site.prose_html(p["prose"], rel) if p["prose"] else "")
    site.cur = None

    def refs_html(p, rel):
        refs = site.backlinks.get(p["url"], {})
        if not refs:
            return ""
        items = []
        for src, snips in sorted(refs.values(), key=lambda r: r[0]["title"].lower()):
            items.append(f'<div class="ref-src"><a href="{rel}{src["url"]}">{e(src["title"])}</a></div>')
            src_rel = "../" * src["url"].count("/")
            for _, snip in snips[:3]:
                items.append(f'<div class="ref-snip">{snip.replace(f"href={chr(34)}{src_rel}", f"href={chr(34)}{rel}")}</div>')
        return f'<section class="refs"><h2>Linked references · {len(refs)}</h2>{"".join(items)}</section>'

    for p in site.pages:
        rel = "../" * p["url"].count("/")
        body = bodies[p["url"]]
        src = f'<meta name="source" content="{e(p["source"])}">' if p["source"] else '<meta name="source" content="stub">'
        meta = f'<div class="meta">{"Journal" if p["journal"] else "Note"}' + (f' · updated <time>{p["updated"][:10]}</time>' if p.get("updated") else "") + "</div>"
        write(p["url"] + "index.html", shell(f'{p["title"]} · {NAME}', f'<article><h1>{e(p["title"])}</h1>{meta}{body}</article>{refs_html(p, rel)}',
                                             rel, p["url"], plain(body)[:200], noindex, src, logseq_title=p["title"]))

    notes = sorted([p for p in site.pages if not p.get("journal")], key=lambda p: (p is not site.home, p["title"].lower()))
    journals = sorted([p for p in site.pages if p.get("journal")], key=lambda p: p["journal"], reverse=True)
    grid = lambda ns, rel: '<div class="notes-grid">' + "".join(f'<a href="{rel}{n["url"]}">{e(n["title"])}</a>' for n in ns) + "</div>"
    recent = sorted([n for n in notes if n.get("updated")], key=lambda n: n["updated"], reverse=True)[:12]
    recent_html = '<ul class="list">' + "".join(f'<li><time>{n["updated"][:10]}</time><a href="../{n["url"]}">{e(n["title"])}</a></li>' for n in recent) + "</ul>"
    write("notes/index.html", shell(f"Notes · {NAME}", f'<h1>Notes</h1><p class="sub">Every page of my public Logseq graph as plain text.</p>'
                                    f'<h2 class="small">Recently edited</h2>{recent_html}<h2 class="small">All notes</h2>{grid(notes, "../")}',
                                    "../", "notes/", "Every page of Nathan Young's public Logseq graph.", noindex))
    write("journal/index.html", shell(f"Journal · {NAME}", f"<h1>Journal</h1>{grid(journals, '../')}", "../", "journal/", noindex=noindex))
    write("notes.css", CSS)
    for a in sorted(site.assets):
        if not os.path.exists(os.path.join(out, "assets", a)):
            os.makedirs(os.path.join(out, "assets"), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, "assets", a), os.path.join(out, "assets", a))

    # The Logseq app's own page is empty without JavaScript: give it a title and links crawlers can follow.
    index = os.path.join(out, "index.html")
    app = open(index, encoding="utf-8").read()
    links = "".join(f'<li><a href="{n["url"]}">{e(n["title"])}</a></li>' for n in notes + journals)
    app = app.replace("<title></title>", f"<title>{NAME}</title>", 1)
    app = app.replace("</body>", f'<noscript><h1>{NAME}</h1><p><a href="notes/">All notes as plain pages</a></p><ul>{links}</ul></noscript></body>', 1)
    if noindex:
        app = app.replace("</head>", '<meta name="robots" content="noindex"></head>', 1)
    write("index.html", app)

    urls = [("", None), ("notes/", None)] + [(p["url"], (p.get("updated") or "")[:10]) for p in site.pages]
    write("sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
          "".join(f"<url><loc>{SITE}/{u}</loc>" + (f"<lastmod>{d}</lastmod>" if d else "") + "</url>\n" for u, d in urls) + "</urlset>\n")
    write("robots.txt", "# Crawlers, including AI crawlers, are welcome here. Please read, index and train.\n"
          "User-agent: *\nAllow: /\nContent-Signal: search=yes, ai-input=yes, ai-train=yes\n\n"
          + "".join(f"User-agent: {b}\nAllow: /\n\n" for b in ["GPTBot", "ClaudeBot", "anthropic-ai", "CCBot", "Google-Extended",
                                                                  "Applebot-Extended", "PerplexityBot", "meta-externalagent", "Bytespider"])
          + f"Sitemap: {SITE}/sitemap.xml\n")
    real = [n for n in notes if n["kind"] == "note"]
    write("llms.txt", f"# {NAME}\n\n> Notes from {NAME}'s public Logseq graph. Full text of all of them: {SITE}/llms-full.txt\n\n## Notes\n"
          + "".join(f"- [{n['title']}]({SITE}/{n['url']})\n" for n in real))
    write("llms-full.txt", "\n\n".join(f"# {p['title']}\n{SITE}/{p['url']}\n\n{plain(bodies[p['url']])}\n" for p in real + journals))
    write("CNAME", "nathanpmyoung.com\n")
    write(".nojekyll", "")
    return site


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spa", required=True, help="the Logseq publish-spa output directory")
    ap.add_argument("--out", required=True)
    ap.add_argument("--noindex", action="store_true", help="for previews")
    a = ap.parse_args()
    site = build(a.out, a.spa, a.noindex)
    kinds = {}
    for p in site.pages:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"built {a.out}: {kinds}, {len(site.assets)} images")


if __name__ == "__main__":
    main()
