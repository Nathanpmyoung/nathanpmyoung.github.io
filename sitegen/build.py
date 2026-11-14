#!/usr/bin/env python3
"""Build nathanpmyoung.com as plain static HTML from this Logseq graph plus the Substack posts.

python3 sitegen/build.py --out www                     # fetch Substack live
python3 sitegen/build.py --out www --posts posts.json  # reuse a saved fetch
Only reads the graph this script sits in; sitegen/gate.py checks the output.
"""
import argparse, datetime as dt, html, json, os, re, shutil, subprocess, sys, urllib.parse, urllib.request
from email.utils import format_datetime
from html.parser import HTMLParser

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


# ---------- Substack ----------

def fetch_posts():
    posts, cursor = [], None
    while True:
        u = f"{SUBSTACK}/api/v1/posts?limit=20" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        req = urllib.request.Request(u, headers={"User-Agent": "nathanpmyoung.com site builder"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        posts += d["posts"]
        cursor = d.get("nextCursor")
        if not cursor or not d["posts"]:
            break
    return [p for p in posts if p.get("audience") == "everyone" and p.get("body_html") and p.get("is_published", True)]


class Clean(HTMLParser):
    """Keep the prose of a Substack post; drop widgets, scripts, styles and unknown attributes."""
    KEEP = {"p", "a", "em", "strong", "b", "i", "u", "s", "ul", "ol", "li", "blockquote", "h1", "h2", "h3", "h4", "h5",
            "h6", "img", "figure", "figcaption", "pre", "code", "hr", "br", "table", "thead", "tbody", "tr", "th", "td",
            "sup", "sub", "div"}
    DROP = {"script", "style", "button", "form", "input", "svg", "iframe", "noscript", "source", "template", "select"}
    DROP_CLASS = ("subscription-widget", "button-wrapper", "subscribe", "share", "image-link-expand", "pencraft", "tweet",
                  "embedded-post", "poll-embed", "digest-post-embed", "install-substack")
    VOID = {"img", "br", "hr", "source", "input"}

    def __init__(self, link):
        super().__init__(convert_charrefs=False)
        self.out, self.skip, self.link = [], [], link

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            if tag not in self.VOID:
                self.skip.append(tag)
            return
        if tag in self.DROP or any(c in (a.get("class") or "") for c in self.DROP_CLASS):
            try:
                url = json.loads(a.get("data-attrs") or "{}").get("url") or ""
            except (ValueError, AttributeError):
                url = ""
            if url.startswith("https://") and "subscribe" not in url:
                self.out.append(f'<p><a class="media" href="{e(self.link(url))}">{e(url)}</a></p>')
            if tag not in self.VOID:
                self.skip.append(tag)
            return
        if tag not in self.KEEP:
            return
        keep = []
        if tag == "a" and a.get("href", "").startswith(("http://", "https://", "mailto:", "#")):
            keep.append(("href", self.link(a["href"])))
        if tag == "img":
            if not (a.get("src") or "").startswith("https://"):
                return
            keep += [("src", a["src"]), ("alt", a.get("alt") or ""), ("loading", "lazy")]
        if a.get("id") and re.fullmatch(r"[\w-]+", a["id"]):
            keep.append(("id", a["id"]))
        if tag == "div" and "footnote" in (a.get("class") or ""):
            keep.append(("class", "footnote"))
        self.out.append(f"<{tag}" + "".join(f' {k}="{e(v)}"' for k, v in keep) + ">")

    def handle_endtag(self, tag):
        if self.skip:
            if self.skip[-1] == tag:
                self.skip.pop()
            return
        if tag in self.KEEP and tag not in self.VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(e(d, quote=False))

    def handle_entityref(self, n):
        if not self.skip:
            self.out.append(f"&{n};")

    def handle_charref(self, n):
        if not self.skip:
            self.out.append(f"&#{n};")


# ---------- the site ----------

class Site:
    def __init__(self, notes, posts):
        self.pages, self.by_name, self.backlinks, self.assets, self.blocks_by_id = [], {}, {}, set(), {}
        used = set()

        def claim(base):
            u, n = base, 2
            while u in used:
                u, n = f"{base[:-1]}-{n}/", n + 1
            used.add(u)
            return u

        for p in posts:
            date = p["post_date"][:10]
            self.add(dict(kind="post", title=p["title"], subtitle=p.get("subtitle") or "", date=date,
                          updated=p.get("updated_at") or p["post_date"], url=claim(f"posts/{slugify(p['slug'])}/"),
                          canonical=p.get("canonical_url") or f"{SUBSTACK}/p/{p['slug']}", raw=p["body_html"],
                          source=f"{SUBSTACK}/p/{p['slug']}", slug=p["slug"], aliases=[]))
        self.post_by_slug = {p["slug"]: p for p in self.pages}
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
        m = re.match(r"https?://(?:www\.)?nathanpmyoung\.substack\.com/p/([\w-]+)", url)
        if m and m.group(1) in self.post_by_slug:
            post = self.post_by_slug[m.group(1)]
            if self.cur is not None and self.counting is None and post is not self.cur:
                self.backlinks.setdefault(post["url"], {}).setdefault(self.cur["url"], (self.cur, []))
            return f'<a class="ref" href="{rel}{post["url"]}">{label}</a>'
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

    def post_link(self, href):
        m = re.match(r"https?://(?:www\.)?nathanpmyoung\.substack\.com/p/([\w-]+)(#.*)?$", href)
        if m and m.group(1) in self.post_by_slug:
            p = self.post_by_slug[m.group(1)]
            if p is not self.cur:
                self.backlinks.setdefault(p["url"], {}).setdefault(self.cur["url"], (self.cur, []))
            return "../../" + p["url"] + (m.group(2) or "")
        return href


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
.post figure{margin:1.2rem 0}.post figcaption{color:var(--mute);font-size:.85rem}.footnote{font-size:.9rem;color:var(--mute)}
section.refs{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:1rem}
section.refs h2,.cols h2{font:600 .8rem system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
.ref-src{margin:.9rem 0 .2rem;font-weight:600}.ref-snip{background:var(--card);border-radius:4px;padding:.3rem .7rem;margin:.2rem 0;font-size:.95rem}
.list{list-style:none;padding:0;margin:0}.list li{margin:.35rem 0;display:flex;gap:.8rem}.list time{color:var(--mute);font:.82rem/1.9 system-ui,sans-serif;white-space:nowrap;min-width:5.6rem}
.notes-grid{columns:15rem;column-gap:1.5rem;font-size:.95rem}.notes-grid a{display:block;break-inside:avoid;padding:.1rem 0}
.cols{display:grid;grid-template-columns:1fr;gap:2rem;margin-top:2.2rem}@media (min-width:900px){body{max-width:62rem}.cols{grid-template-columns:3fr 2fr}}
.kind{font:.7rem system-ui,sans-serif;text-transform:uppercase;letter-spacing:.05em;color:var(--mute)}
footer{margin-top:3rem;color:var(--mute);font:.8rem system-ui,sans-serif}
"""

LEGACY_JS = """<script>(function(){var h=location.hash.match(/^#\\/page\\/(.+)$/);if(!h)return;var m=%s;
var k=decodeURIComponent(h[1]).toLowerCase();if(m[k])location.replace(m[k]);})();</script>"""


def shell(site, page_title, body, rel, url, desc="", canonical=None, noindex=False, extra_head=""):
    canon = canonical or f"{SITE}/{url}"
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(page_title)}</title><meta name="description" content="{e(desc[:200])}">
<link rel="canonical" href="{e(canon)}">{robots}<meta name="author" content="{NAME}">
<meta property="og:title" content="{e(page_title)}"><meta property="og:description" content="{e(desc[:200])}"><meta property="og:url" content="{e(canon)}">
<link rel="alternate" type="application/rss+xml" title="{NAME}" href="{rel}feed.xml">
<link rel="stylesheet" href="{rel}style.css">{extra_head}</head><body>
<header class="site"><a class="me" href="{rel}">{NAME}</a><a href="{rel}posts/">Posts</a><a href="{rel}notes/">Notes</a><a href="{rel}journal/">Journal</a><a href="{SUBSTACK}">Substack</a><a href="{rel}feed.xml">RSS</a></header>
<main>{body}</main>
<footer>Notes from Nathan's public <a href="https://logseq.com">Logseq</a> graph and posts from <a href="{SUBSTACK}">his Substack</a>.{" Built " + site.built + "." if rel == "" and url == "" else ""}</footer>
</body></html>"""


def plain(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def build(out, posts, noindex=False):
    site = Site(load_graph(), posts)
    site.built = dt.date.today().isoformat()
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)

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
        if p["kind"] == "post":
            c = Clean(lambda h: site.post_link(h))
            c.feed(p["raw"])
            c.close()
            bodies[p["url"]] = "".join(c.out)
        else:
            bodies[p["url"]] = site.render_blocks(p["blocks"], rel) + (site.prose_html(p["prose"], rel) if p["prose"] else "")
    site.cur = None

    def refs_html(p, rel):
        refs = site.backlinks.get(p["url"], {})
        if not refs:
            return ""
        items = []
        for src, snips in sorted(refs.values(), key=lambda r: (r[0]["kind"] != "post", r[0]["title"].lower())):
            items.append(f'<div class="ref-src"><a href="{rel}{src["url"]}">{e(src["title"])}</a> <span class="kind">{"post" if src["kind"] == "post" else ""}</span></div>')
            src_rel = "../" * src["url"].count("/")
            for _, snip in snips[:3]:
                items.append(f'<div class="ref-snip">{snip.replace(f"href={chr(34)}{src_rel}", f"href={chr(34)}{rel}")}</div>')
        return f'<section class="refs"><h2>Linked references · {len(refs)}</h2>{"".join(items)}</section>'

    for p in site.pages:
        rel = "../" * p["url"].count("/")
        body = bodies[p["url"]]
        desc = plain(body)[:200]
        if p["kind"] == "post":
            head = f'<h1>{e(p["title"])}</h1>' + (f'<p class="sub">{e(p["subtitle"])}</p>' if p["subtitle"] else "")
            meta = f'<div class="meta"><time datetime="{p["date"]}">{p["date"]}</time> · first published on <a href="{e(p["canonical"])}">Substack</a></div>'
            html_ = shell(site, p["title"], f'<article class="post">{head}{meta}{body}</article>{refs_html(p, rel)}', rel, p["url"],
                          p["subtitle"] or desc, canonical=p["canonical"], noindex=noindex,
                          extra_head=f'<meta name="source" content="substack:{e(p["slug"])}">')
        else:
            src = f'<meta name="source" content="{e(p["source"])}">' if p["source"] else '<meta name="source" content="stub">'
            meta = f'<div class="meta">{"Journal" if p["journal"] else "Note"}' + (f' · updated <time>{p["updated"][:10]}</time>' if p.get("updated") else "") + "</div>"
            html_ = shell(site, f'{p["title"]} · {NAME}' if p is not site.home else NAME,f'<article><h1>{e(p["title"])}</h1>{meta}{body}</article>{refs_html(p, rel)}',
                          rel, p["url"], desc, noindex=noindex, extra_head=src)
        write(p["url"] + "index.html", html_)

    posts_sorted = sorted([p for p in site.pages if p["kind"] == "post"], key=lambda p: p["date"], reverse=True)
    notes = sorted([p for p in site.pages if p["kind"] in ("note", "stub") and not p.get("journal") and p is not site.home], key=lambda p: p["title"].lower())
    journals = sorted([p for p in site.pages if p.get("journal")], key=lambda p: p["journal"], reverse=True)

    def post_list(ps, rel):
        return '<ul class="list">' + "".join(f'<li><time>{p["date"]}</time><a href="{rel}{p["url"]}">{e(p["title"])}</a></li>' for p in ps) + "</ul>"

    def note_grid(ns, rel):
        return '<div class="notes-grid">' + "".join(f'<a href="{rel}{n["url"]}">{e(n["title"])}</a>' for n in ns) + "</div>"

    redirect_map = {}
    for p in site.pages:
        if p["kind"] != "post":
            for name in [p["title"]] + p["aliases"]:
                redirect_map.setdefault(name.lower(), p["url"])
    home_body = re.sub(r'(href|src)="\.\./\.\./', r'\1="', bodies[site.home["url"]]) if site.home else ""
    recent_notes = sorted([n for n in notes if n.get("updated")], key=lambda n: n["updated"], reverse=True)[:12]
    recent = "".join(f'<li><time>{n["updated"][:10]}</time><a href="{n["url"]}">{e(n["title"])}</a></li>' for n in recent_notes)
    home = (f"<h1>{NAME}</h1>{home_body}<div class=\"cols\"><section><h2>Recent posts</h2>{post_list(posts_sorted[:15], '')}"
            f'<p><a href="posts/">All {len(posts_sorted)} posts →</a></p></section><section><h2>Recently edited notes</h2>'
            f'<ul class="list">{recent}</ul>'
            f'<p><a href="notes/">All {len(notes)} notes →</a></p></section></div>')
    write("index.html", shell(site, NAME, home, "", "", "Nathan Young's notes and essays on forecasting, AI, discourse and more.", noindex=noindex,
                              extra_head=LEGACY_JS % json.dumps(redirect_map)))
    write("posts/index.html", shell(site, f"Posts · {NAME}", f"<h1>Posts</h1>{post_list(posts_sorted, '../')}", "../", "posts/", noindex=noindex))
    write("notes/index.html", shell(site, f"Notes · {NAME}", f"<h1>Notes</h1><p class=\"sub\">Pages from my public Logseq graph. Links between them work like Logseq's.</p>{note_grid(notes, '../')}", "../", "notes/", noindex=noindex))
    write("journal/index.html", shell(site, f"Journal · {NAME}", f"<h1>Journal</h1>{note_grid(journals, '../')}", "../", "journal/", noindex=noindex))
    write("style.css", CSS)
    if site.assets:
        os.makedirs(os.path.join(out, "assets"))
        for a in sorted(site.assets):
            shutil.copy2(os.path.join(ROOT, "assets", a), os.path.join(out, "assets", a))

    # Crawlers: sitemap, feed, robots, llms.txt.
    urls = [("", site.built)] + [(p["url"], (p.get("updated") or "")[:10]) for p in site.pages]
    write("sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
          "".join(f"<url><loc>{SITE}/{u}</loc>" + (f"<lastmod>{d}</lastmod>" if d else "") + "</url>\n" for u, d in urls) + "</urlset>\n")
    items = "".join(
        f"<item><title>{e(p['title'])}</title><link>{SITE}/{p['url']}</link><guid isPermaLink=\"false\">{e(p['canonical'])}</guid>"
        f"<pubDate>{format_datetime(dt.datetime.fromisoformat(p['date']).replace(tzinfo=dt.timezone.utc))}</pubDate>"
        f"<description>{e(bodies[p['url']].replace('href=\"../../', f'href=\"{SITE}/'))}</description></item>\n" for p in posts_sorted[:50])
    write("feed.xml", f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>{NAME}</title><link>{SITE}/</link>'
          f"<description>Essays and notes by {NAME}</description>\n{items}</channel></rss>\n")
    write("robots.txt", "# Crawlers, including AI crawlers, are welcome here. Please read, index and train.\n"
          "User-agent: *\nAllow: /\nContent-Signal: search=yes, ai-input=yes, ai-train=yes\n\n"
          + "".join(f"User-agent: {b}\nAllow: /\n\n" for b in ["GPTBot", "ClaudeBot", "anthropic-ai", "CCBot", "Google-Extended",
                                                                  "Applebot-Extended", "PerplexityBot", "meta-externalagent", "Bytespider"])
          + f"Sitemap: {SITE}/sitemap.xml\n")
    llms = [f"# {NAME}\n\n> Notes (public Logseq graph) and essays (Substack) by {NAME}. Full text of everything: {SITE}/llms-full.txt\n\n## Posts\n"]
    llms += [f"- [{p['title']}]({SITE}/{p['url']})\n" for p in posts_sorted]
    llms += ["\n## Notes\n"] + [f"- [{n['title']}]({SITE}/{n['url']})\n" for n in notes if n["kind"] == "note"]
    write("llms.txt", "".join(llms))
    full = []
    for p in posts_sorted + [n for n in notes if n["kind"] == "note"] + ([site.home] if site.home else []):
        full.append(f"# {p['title']}\n{SITE}/{p['url']}\n\n{plain(bodies[p['url']])}\n")
    write("llms-full.txt", "\n\n".join(full))
    write("CNAME", "nathanpmyoung.com\n")
    write(".nojekyll", "")
    return site


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--posts", help="JSON file of Substack posts; fetched and saved there if missing")
    ap.add_argument("--noindex", action="store_true", help="for previews")
    a = ap.parse_args()
    if a.posts and os.path.exists(a.posts):
        posts = json.load(open(a.posts))
    else:
        posts = fetch_posts()
        if a.posts:
            json.dump(posts, open(a.posts, "w"))
    if len(posts) < 50:
        sys.exit(f"only {len(posts)} Substack posts fetched; refusing to build a site missing most of them")
    site = build(a.out, posts, a.noindex)
    kinds = {}
    for p in site.pages:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"built {a.out}: {kinds}, {len(site.assets)} images")


if __name__ == "__main__":
    main()
