"""The privacy gate must fail on each way a private page could ship. python3 -m unittest sitegen/test_gate.py"""
import os, subprocess, sys, tempfile, unittest

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate.py")
OK_NOTE = '<meta name="source" content="pages/AI.md"><article>fine</article>'


def run(files, private=None):
    with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as pdir:
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(out, rel)) or out, exist_ok=True)
            open(os.path.join(out, rel), "w").write(text)
        args = [sys.executable, GATE, "--out", out]
        if private:
            open(os.path.join(pdir, "secret.md"), "w").write(f"- {private}\n")
            args += ["--private", pdir]
        r = subprocess.run(args, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


class Gate(unittest.TestCase):
    def test_public_note_passes(self):
        code, out = run({"notes/ai/index.html": OK_NOTE, "index.html": ""})
        self.assertEqual(code, 0, out)

    def test_fails(self):
        secret = "This sentence only exists in the private graph and must never be published anywhere."
        cases = {
            "untracked source": {"notes/x/index.html": '<meta name="source" content="pages/Secret diary.md">'},
            "no source tag": {"notes/x/index.html": "<article>hi</article>"},
            "unexpected file": {"private/notes.md": "x"},
            "stub with content": {"notes/x/index.html": '<meta name="source" content="stub"><ul class="blocks"><li>x</li></ul>'},
            "stub with prose": {"notes/x/index.html": '<meta name="source" content="stub"><article><h1>x</h1><p>diary</p></article>'},
            "hidden file": {"notes/.x.html": "diary"},
            "post without substack": {"posts/x/index.html": '<meta name="source" content="pages/AI.md">'},
        }
        for name, files in cases.items():
            with self.subTest(name):
                code, out = run(files)
                self.assertEqual(code, 1, out)
        with self.subTest("private text"):
            code, out = run({"notes/ai/index.html": OK_NOTE + f"<p>{secret}</p>"}, private=secret)
            self.assertEqual(code, 1, out)


if __name__ == "__main__":
    unittest.main()
