import re
import unittest
from pathlib import Path


class SkillStructureTests(unittest.TestCase):
    def test_skill_frontmatter_and_linked_files(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        closing = content.find("\n---\n", 4)
        self.assertGreater(closing, 4)
        frontmatter = content[4:closing]
        name = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
        description = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
        if name is None or description is None:
            self.fail("SKILL.md requires name and description frontmatter")
        self.assertEqual(name.group(1).strip(), "xhs-workflow")
        self.assertLessEqual(len(description.group(1).strip()), 1024)
        self.assertTrue(content[closing + 5 :].strip())
        self.assertLessEqual(len(content), 100000)

        linked = [
            "scripts/xhs_workflow.py",
            "scripts/xhs_readonly_adapter.py",
            "templates/post.json",
            "templates/readonly-request.json",
            "templates/publication-result.json",
            "references/backends.md",
        ]
        for relative in linked:
            with self.subTest(relative=relative):
                self.assertTrue((root / relative).is_file())


if __name__ == "__main__":
    unittest.main()
