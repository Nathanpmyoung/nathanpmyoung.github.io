#!/usr/bin/env python3
"""Fail if the built site could publish anything that is not already public.

The rule: a note may ship only if its file is committed and pushed to the public GitHub repo
nathanpmyoung/nathanpmyoung.github.io and is not marked `public:: false`. Shares no code with
build.py on purpose; it reads the output and asks git directly.

python3 sitegen/gate.py --out www [--private DIR]
--private DIR: also fail if any line from a private Logseq export (that is not also in the
public repo or a published Substack post) appears anywhere in the output. Used on hq; the
export never leaves the box.
"""
import argparse, glob, html, os, re, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_REMOTE = re.compile(r"^(https://|git@|ssh://git@)github\.com[:/]nathanpmyoung/nathanpmyoung\.github\.io(\.git)?/?$", re.I)
INDEXES = {"index.html", "posts/index.html", "notes/index.html", "journal/index.html"}
FILES = {"style.css", "feed.xml", "sitemap.xml", "robots.txt", "llms.txt", "llms-full.txt", "CNAME", ".nojekyll"}
NOT_PUBLIC = re.compile(r"^\s*-?\s*public::\s*false", re.I | re.M)


def git(*args, text=True):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=text, check=True).stdout


def flat(s):
    """Text without markup, entities or runs of whitespace, so HTML and Markdown compare."""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s.replace("[[", "").replace("]]", ""))
    s = html.unescape(html.unescape(re.sub(r"<[^>]+>", " ", s)))
    return re.sub(r"\s+", " ", re.sub(r"[*_`#]", "", s)).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--private")
    a = ap.parse_args()
    bad = []

    if re.search(r"/streams/[^/]+/notes/", os.path.realpath(REPO) + "/"):
        bad.append(f"builder is running inside a private export: {REPO}")
    if not PUBLIC_REMOTE.match(git("remote", "get-url", "origin").strip()):
        bad.append("origin is not github.com/nathanpmyoung/nathanpmyoung.github.io")
    git("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    # Every graph file must be exactly what is on the public main branch.
    dirty = git("status", "--porcelain", "--untracked-files=all", "--", "pages", "journals", "assets", "*.md").strip()
    unpushed = git("diff", "--name-only", "origin/main", "HEAD", "--", "pages", "journals", "assets", "*.md").strip()
    if dirty:
        bad.append("uncommitted graph files:\n  " + dirty.replace("\n", "\n  "))
    if unpushed:
        bad.append("graph files differ from public origin/main:\n  " + unpushed.replace("\n", "\n  "))
    public, links = set(), set()
    for line in git("ls-tree", "-r", "origin/main").splitlines():
        meta, name = line.split("\t", 1)
        (links if meta.startswith("120000") else public).add(name)  # a symlink could point at a private file
    if links:
        bad.append("public repo has symlinks; refusing: " + ", ".join(sorted(links)[:5]))

    for path in sorted(os.path.join(d, f) for d, _, fs in os.walk(a.out) for f in fs):  # os.walk sees dotfiles
        rel = os.path.relpath(path, a.out)
        if os.path.islink(path):
            bad.append(f"{rel}: symlink in output")
            continue
        if rel in INDEXES or rel in FILES:
            continue
        if rel.startswith("assets/"):
            if rel not in public:
                bad.append(f"{rel}: image not in public repo")
            elif open(path, "rb").read() != git("show", f"origin/main:{rel}", text=False):
                bad.append(f"{rel}: image differs from the public repo copy")
            continue
        m = re.fullmatch(r"(posts|notes|journal)/[a-z0-9-]+/index\.html", rel)
        if not m:
            bad.append(f"{rel}: unexpected file")
            continue
        page = open(path, encoding="utf-8").read()
        src = re.findall(r'<meta name="source" content="([^"]*)">', page)
        if len(src) != 1:
            bad.append(f"{rel}: no single source tag")
            continue
        src = html.unescape(src[0])
        if m.group(1) == "posts":
            if not re.fullmatch(r"substack:[\w-]+", src):
                bad.append(f"{rel}: post without a Substack source")
            continue
        if src == "stub":
            own = re.search(r"<article>(.*?)</article>", page, re.S)
            if not own or flat(re.sub(r'<h1>.*?</h1>|<div class="meta">.*?</div>', "", own.group(1), flags=re.S)):
                bad.append(f"{rel}: stub page carries its own content")
            continue
        if src not in public:
            bad.append(f"{rel}: source {src} is not in the public repo")
            continue
        if NOT_PUBLIC.search(git("show", f"origin/main:{src}")):
            bad.append(f"{rel}: source {src} is marked public:: false")

    if a.private:
        public_text = set()
        for f in public:
            if f.endswith(".md") and not f.startswith("logseq/"):
                body = git("show", f"origin/main:{f}")
                if not NOT_PUBLIC.search(body):
                    public_text.update(flat(re.sub(r"^[-\s]+", "", l)) for l in body.splitlines())
        # Drafts he later published on Substack are public through the post itself.
        posted = flat("".join(open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(a.out, "posts", "*", "index.html"))))
        canaries = set()
        for f in glob.glob(os.path.join(a.private, "**", "*.md"), recursive=True):
            for l in open(f, encoding="utf-8", errors="replace"):
                t = flat(re.sub(r"^[-\s]+", "", l))
                if len(t) >= 60 and t not in public_text:
                    canaries.add(t)
        out_text = ""
        for d, _, fs in os.walk(a.out):
            for f in fs:
                if f.endswith((".html", ".txt", ".xml")):
                    out_text += "\n" + flat(open(os.path.join(d, f), encoding="utf-8").read())
        leaks = [c for c in canaries if c in out_text and c not in posted]
        if leaks:
            bad.append(f"{len(leaks)} private lines appear in the output, e.g.:\n  " + "\n  ".join(l[:90] for l in leaks[:5]))
        print(f"checked {len(canaries)} private lines against the output")

    if bad:
        print("GATE FAILED: something not public would ship\n- " + "\n- ".join(bad))
        sys.exit(1)
    print("gate ok: every note is from the public repo; nothing private found")


if __name__ == "__main__":
    main()
