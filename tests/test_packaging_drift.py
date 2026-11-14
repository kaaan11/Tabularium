"""Packaging drift regression tests (BUG-11 / F9)."""

import re
import tomllib
import unittest
from pathlib import Path

from tabularium.schema_resources import SCHEMA_NAMES, schema_text

ROOT = Path(__file__).resolve().parents[1]


class TestPackagingDrift(unittest.TestCase):
    def test_packaged_schemas_match_normative_sources(self):
        for name in SCHEMA_NAMES:
            with self.subTest(name=name):
                self.assertEqual(schema_text(name), (ROOT / "tabularium" / "schemas" / f"{name}.schema.yaml").read_text(encoding="utf-8"))

    def test_unknown_schema_refused(self):
        with self.assertRaises(ValueError):
            schema_text("../VERSION")

    def test_pyproject_packages_match_the_source_tree(self):
        """Every importable package under tabularium/ is declared.

        F9 was exactly this drift.
        """
        with open(ROOT / "pyproject.toml", "rb") as f:
            declared = set(tomllib.load(f)["tool"]["setuptools"]["packages"])
        roots = [ROOT / "tabularium"]
        on_disk = {
            str(p.parent.relative_to(ROOT)).replace("/", ".")
            for r in roots
            for p in r.rglob("__init__.py")
        }
        self.assertEqual(on_disk - declared, set())


ENGINE = ROOT / "tabularium" / "engine"
GUIDES = (ROOT / "README.md", ENGINE / "README.md",
          ROOT / "examples" / "demo-project" / "README.md")
_REQUIREMENT_FILE_EXTRA = {
    "requirements.txt": None,  # base `dependencies`
    "requirements-api.txt": "api",
    "requirements-runtime.txt": "runtime",
    "requirements-observability.txt": "observability",
    "requirements-dev.txt": "dev",
}


def _engine_readme() -> str:
    return (ENGINE / "README.md").read_text(encoding="utf-8")


class TestDependencyDocsDrift(unittest.TestCase):
    """Docs name only files that exist, and point at pyproject.toml (#7)."""

    def _table_paths(self) -> list[str]:
        paths = []
        for line in _engine_readme().splitlines():
            if line.startswith("| `"):
                first_cell = line.split("|")[1]
                paths += re.findall(r"`([^`]+/)`", first_cell)
        return paths

    def test_engine_readme_component_paths_exist(self):
        paths = self._table_paths()
        self.assertTrue(paths)
        self.assertEqual([p for p in paths if not (ENGINE / p).is_dir()], [])

    def test_every_engine_package_is_mapped(self):
        mapped = {p.rstrip("/") for p in self._table_paths()}
        packages = {p.parent.relative_to(ENGINE).as_posix()
                    for p in ENGINE.rglob("__init__.py") if p.parent != ENGINE}
        self.assertEqual(packages - mapped, set())

    def test_engine_readme_names_pyproject_and_only_existing_files(self):
        text = _engine_readme()
        self.assertIn("pyproject.toml", text)
        named = set(re.findall(r"`(requirements[-a-z]*\.txt)`", text))
        self.assertEqual({n for n in named if not (ENGINE / n).is_file()}, set())

    def test_engine_dependency_groups_match_pyproject(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            extras = tomllib.load(f)["project"]["optional-dependencies"]
        groups = set(re.findall(r"^\| `(base|[a-z]+)` \|", _engine_readme(), re.M))
        self.assertEqual(groups, {"base", *extras})

    def test_requirement_files_are_declared_in_pyproject(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            project = tomllib.load(f)["project"]
        for name, extra in _REQUIREMENT_FILE_EXTRA.items():
            declared = set(project["dependencies"] if extra is None
                           else project["optional-dependencies"][extra])
            lines = (ENGINE / name).read_text(encoding="utf-8").splitlines()
            listed = {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}
            with self.subTest(file=name):
                self.assertEqual(listed - declared, set())
                self.assertIn("pyproject.toml", "\n".join(lines))

    def test_documented_install_extras_exist(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            extras = tomllib.load(f)["project"]["optional-dependencies"]
        paths = (*GUIDES, *(ENGINE / name for name in _REQUIREMENT_FILE_EXTRA))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            installs = re.findall(r'pip install -e (?:"\.\[([^\]]+)\]"|(\.)(?=\s|$))', text)
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(installs, "Document installation from pyproject.toml")
                for names, _ in installs:
                    if names:
                        self.assertEqual(set(names.split(",")) - extras.keys(), set())

    def test_guide_local_links_exist(self):
        for guide in GUIDES:
            links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", guide.read_text(encoding="utf-8"))
            for link in links:
                if "://" in link or link.startswith("#"):
                    continue
                with self.subTest(guide=guide.relative_to(ROOT), link=link):
                    self.assertTrue((guide.parent / link.split("#")[0]).exists())

    def test_demo_python_requirement_matches_product(self):
        with open(ROOT / "pyproject.toml", "rb") as f:
            product = tomllib.load(f)["project"]["requires-python"]
        with open(ROOT / "examples" / "demo-project" / "pyproject.toml", "rb") as f:
            demo = tomllib.load(f)["project"]["requires-python"]
        self.assertEqual(demo, product)

    def test_root_readme_example_paths_exist(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        named = set(re.findall(r"(examples/[A-Za-z0-9_./-]+)", text))
        self.assertTrue(named)
        self.assertEqual({n for n in named if not (ROOT / n).exists()}, set())


if __name__ == "__main__":
    unittest.main()
