import unittest

from src.claude.bash_outputs import extract_bash_outputs


class BashOutputsTests(unittest.TestCase):
    def test_redirect(self):
        self.assertEqual(extract_bash_outputs("cat header > report.md"), ["report.md"])
        self.assertEqual(extract_bash_outputs("echo hi >> log.txt"), ["log.txt"])
        self.assertEqual(extract_bash_outputs("foo >out.json"), ["out.json"])

    def test_tee(self):
        self.assertEqual(extract_bash_outputs("echo x | tee a.txt"), ["a.txt"])
        self.assertEqual(extract_bash_outputs("echo x | tee -a b.txt"), ["b.txt"])

    def test_cp_mv_dest_last(self):
        self.assertEqual(
            extract_bash_outputs("cp index.html index-copy.html"),
            ["index-copy.html"],
        )
        self.assertEqual(
            extract_bash_outputs("cp -r content/lesson content/lesson-copy"),
            ["content/lesson-copy"],
        )
        self.assertEqual(extract_bash_outputs("mv old.txt new.txt"), ["new.txt"])

    def test_touch_mkdir_all(self):
        self.assertEqual(extract_bash_outputs("touch a.py b.py"), ["a.py", "b.py"])
        self.assertEqual(extract_bash_outputs("mkdir -p out/sub"), ["out/sub"])

    def test_ignores_devnull_and_globs(self):
        self.assertEqual(extract_bash_outputs("cmd > /dev/null"), [])
        self.assertEqual(extract_bash_outputs("cp a/*.txt dest/"), ["dest"])  # glob src ignored, dest kept
        self.assertEqual(extract_bash_outputs("echo $X > $OUT"), [])

    def test_multiple_segments(self):
        out = extract_bash_outputs("mkdir build && cp a.html build/a.html")
        self.assertIn("build", out)
        self.assertIn("build/a.html", out)

    def test_non_creating_commands(self):
        self.assertEqual(extract_bash_outputs("ls -la"), [])
        self.assertEqual(extract_bash_outputs("grep foo bar.txt"), [])
        self.assertEqual(extract_bash_outputs("cat file.txt"), [])

    def test_fd_duplication_not_a_file(self):
        # 2>&1 / >&2 — дупликация дескриптора, НЕ файл (фантом &1/&2).
        self.assertEqual(extract_bash_outputs("npm run build > build.log 2>&1"), ["build.log"])
        self.assertEqual(extract_bash_outputs("ls > /dev/null 2>&1"), [])
        self.assertEqual(extract_bash_outputs("pytest 2>&1"), [])
        self.assertEqual(extract_bash_outputs("cmd >&2"), [])

    def test_heredoc_body_not_parsed(self):
        cmd = "cat <<EOF > out.txt\nsome line\ncp evil there\n> notafile\nEOF"
        self.assertEqual(extract_bash_outputs(cmd), ["out.txt"])

    def test_cp_target_directory(self):
        self.assertEqual(extract_bash_outputs("cp -t destdir a.txt b.txt"), ["destdir"])
        self.assertEqual(
            extract_bash_outputs("cp --target-directory=out a.txt b.txt"), ["out"]
        )
        self.assertEqual(
            extract_bash_outputs("install -t bin/ build/app"), ["bin"]
        )

    def test_double_dash(self):
        self.assertEqual(extract_bash_outputs("cp -- -weird.txt dest.txt"), ["dest.txt"])

    def test_empty(self):
        self.assertEqual(extract_bash_outputs(""), [])
        self.assertEqual(extract_bash_outputs("   "), [])


if __name__ == "__main__":
    unittest.main()
