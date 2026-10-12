from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
CLI = SKILL_ROOT / "scripts" / "manage_learning_workspace.py"


def load_cli_module():
    spec = importlib.util.spec_from_file_location("learning_workspace_cli", CLI)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load CLI module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [root, *sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())]
    for path in paths:
        relative_path = path.relative_to(root).as_posix().encode("utf-8")
        metadata = path.lstat()
        if path.is_symlink():
            kind = b"L"
            payload = os.readlink(path).encode("utf-8")
        elif path.is_dir():
            kind = b"D"
            payload = b""
        elif path.is_file():
            kind = b"F"
            payload = path.read_bytes()
        else:
            raise RuntimeError(f"unsupported repository entry: {path}")
        digest.update(kind)
        digest.update(metadata.st_mode.to_bytes(8, "big"))
        digest.update(metadata.st_uid.to_bytes(8, "big"))
        digest.update(metadata.st_gid.to_bytes(8, "big"))
        digest.update(len(relative_path).to_bytes(8, "big"))
        digest.update(relative_path)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def active_stage_count(root: Path) -> int:
    state = json.loads((root / "学习状态.json").read_text(encoding="utf-8"))
    return sum(stage["status"] == "active" for stage in state["stages"])


def stage_marker(state: dict, day: int, bounds: list[int], revision: int) -> dict:
    return {
        "kind": "source-code-learning-stage",
        "marker_version": 1,
        "schema_version": 1,
        "learning_root_id": state["learning_root_id"],
        "day": day,
        "range": bounds,
        "revision": revision,
    }


def source_marker(
    state: dict, stage: Path, root: Path, source: Path, target: Path, order: int, label: str
) -> dict:
    return {
        "kind": "source-code-learning-source",
        "marker_version": 1,
        "schema_version": 1,
        "learning_root_id": state["learning_root_id"],
        "stage_id": stage.relative_to(root).as_posix(),
        "canonical_source_path": str(source.resolve()),
        "relative_learning_dir": target.relative_to(root).as_posix(),
        "order": order,
        "label": label,
    }


def roadmap() -> dict:
    return {
        "title": "Demo 学习路线",
        "stage_ranges": [[0, 30], [31, 60], [61, 100]],
        "days": [
            {
                "day": 1,
                "title": "入口",
                "checkpoints": [
                    {"id": "d1-entry", "title": "读入口", "weight": 20},
                    {"id": "d1-flow", "title": "闭环调用链", "weight": 50},
                    {"id": "d1-summary", "title": "完成总结", "weight": 30},
                ],
            },
            {
                "day": 2,
                "title": "依赖",
                "checkpoints": [
                    {"id": "d2-deps", "title": "读依赖", "weight": 100},
                ],
            },
        ],
    }


class WorkspaceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "demo-project"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
        self.repo_hash = tree_hash(self.repo)
        self.roadmap = self.base / "roadmap.json"
        self.roadmap.write_text(json.dumps(roadmap(), ensure_ascii=False), encoding="utf-8")
        self.roadmap_bytes = self.roadmap.read_bytes()

    def tearDown(self) -> None:
        self.assertTrue(self.repo.is_dir(), "business repository was removed or moved")
        self.assertEqual(tree_hash(self.repo), self.repo_hash, "business repository changed")

    def run_cli(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if ok and result.returncode != 0:
            self.fail(f"CLI failed: {result.stderr}\n{result.stdout}")
        if not ok and result.returncode == 0:
            self.fail(f"CLI unexpectedly passed: {result.stdout}")
        return result

    def run_cli_from(
        self, cwd: Path, *args: str, ok: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if ok and result.returncode != 0:
            self.fail(f"CLI failed: {result.stderr}\n{result.stdout}")
        if not ok and result.returncode == 0:
            self.fail(f"CLI unexpectedly passed: {result.stdout}")
        return result

    def write_roadmap(self, data: dict) -> None:
        self.roadmap.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def init_workspace(self, *extra_args: str) -> Path:
        result = self.run_cli(
            "init",
            "--repo",
            str(self.repo),
            "--roadmap",
            str(self.roadmap),
            *extra_args,
        )
        return Path(result.stdout.strip())

    def assert_no_temp_workspace(self, final_root: Path) -> None:
        prefix = f".{final_root.name}.tmp-"
        leftovers = [path for path in final_root.parent.iterdir() if path.name.startswith(prefix)]
        self.assertEqual(leftovers, [])

    def state_path(self, root: Path) -> Path:
        return root / "学习状态.json"

    def read_state(self, root: Path) -> dict:
        return json.loads(self.state_path(root).read_text(encoding="utf-8"))

    def run_state_command(self, command: str, root: Path, *args: str, ok: bool = True):
        return self.run_cli(command, "--learning-root", str(root), *args, ok=ok)

    def add_source(
        self, root: Path, source: Path, *extra_args: str, stage: Path | None = None,
        ok: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if stage is None:
            state = self.read_state(root)
            stage = root / state["current"]["stage"]["path"]
        return self.run_cli(
            "add-source",
            "--stage",
            str(stage),
            "--source",
            str(source),
            *extra_args,
            ok=ok,
        )

    def register_artifact(
        self,
        root: Path,
        owner_kind: str,
        owner_dir: Path,
        artifact: Path,
        *,
        ok: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "register-artifact",
            "--learning-root",
            str(root),
            "--owner-kind",
            owner_kind,
            "--owner-dir",
            str(owner_dir),
            "--artifact",
            str(artifact),
            ok=ok,
        )

    def test_init_creates_sibling_workspace_and_preserves_repo(self) -> None:
        learning_root = self.init_workspace()
        self.assertEqual(
            learning_root, self.repo.resolve().parent / "demo-project-2天学习路线"
        )
        self.assertTrue((learning_root / "Day01").is_dir())
        self.assertTrue((learning_root / "Day02").is_dir())
        self.assertTrue((learning_root / "学习路线.md").is_file())
        self.assertTrue((learning_root / "学习状态.json").is_file())
        self.assertTrue((learning_root / "学习状态.md").is_file())
        self.assertEqual(tree_hash(self.repo), self.repo_hash)

    def test_templates_project_durable_learning_content(self) -> None:
        root = self.init_workspace()
        self.assertIn(
            "## Day 1", (root / "学习路线.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "当前进度", (root / "学习状态.md").read_text(encoding="utf-8")
        )

        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py", stage=stage).stdout.strip()
        )

        self.assertIn(
            "真实源码路径", (source_dir / "源码导读.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "用户问题", (stage / "问题与解答.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "已闭环流程", (stage / "当日总结.md").read_text(encoding="utf-8")
        )
        overview = stage / "当日总览产物" / "当日统一总结.md"
        self.assertIn("整体框架联系", overview.read_text(encoding="utf-8"))

        for text in (
            (source_dir / "源码导读.md").read_text(encoding="utf-8"),
            overview.read_text(encoding="utf-8"),
        ):
            self.assertEqual(
                text.count("<!-- learning-coach:artifacts:start -->"), 1
            )
            self.assertEqual(text.count("<!-- learning-coach:artifacts:end -->"), 1)

    def test_template_unknown_token_fails_before_creating_output(self) -> None:
        module = load_cli_module()
        template_root = self.base / "template-root"
        template_root.mkdir()
        template = template_root / "unknown-template.md"
        output = self.base / "must-not-exist.md"
        template.write_text("known={{KNOWN}} unknown={{UNKNOWN}}\n", encoding="utf-8")

        with mock.patch.object(module, "TEMPLATE_ROOT", template_root):
            with self.assertRaisesRegex(module.WorkspaceError, "UNKNOWN"):
                module.materialize_template(
                    template.name, output, {"KNOWN": "value"}
                )

        self.assertFalse(output.exists())

    def test_template_substitution_is_single_pass(self) -> None:
        module = load_cli_module()
        template_root = self.base / "template-root"
        template_root.mkdir()
        template = template_root / "single-pass.md"
        template.write_text("{{USER_VALUE}} | {{OTHER}}\n", encoding="utf-8")

        with mock.patch.object(module, "TEMPLATE_ROOT", template_root):
            rendered = module.render_template(
                template.name,
                {"USER_VALUE": "literal {{OTHER}}", "OTHER": "replaced"},
            )

        self.assertEqual(rendered, "literal {{OTHER}} | replaced\n")

    def test_malformed_template_delimiters_fail_before_output(self) -> None:
        module = load_cli_module()
        template_root = self.base / "template-root"
        template_root.mkdir()
        cases = {
            "multiline": "{{UNKNOWN\n}}",
            "empty": "{{}}",
            "unclosed": "{{UNKNOWN",
            "orphan close": "UNKNOWN}}",
            "nested": "{{OUTER{{INNER}}}}",
        }
        with mock.patch.object(module, "TEMPLATE_ROOT", template_root):
            for index, (label, body) in enumerate(cases.items(), start=1):
                with self.subTest(label=label):
                    template_name = f"malformed-{index}.md"
                    (template_root / template_name).write_text(body, encoding="utf-8")
                    output = self.base / f"malformed-output-{index}.md"
                    with self.assertRaisesRegex(module.WorkspaceError, "malformed"):
                        module.materialize_template(template_name, output, {})
                    self.assertFalse(output.exists())

    def test_template_symlink_escape_is_rejected_without_output(self) -> None:
        module = load_cli_module()
        template_root = self.base / "template-root"
        template_root.mkdir()
        external = self.base / "external-template.md"
        external.write_text("{{VALUE}}\n", encoding="utf-8")
        (template_root / "escaped.md").symlink_to(external)
        output = self.base / "escaped-output.md"

        with mock.patch.object(module, "TEMPLATE_ROOT", template_root):
            with self.assertRaisesRegex(module.WorkspaceError, "template"):
                module.materialize_template("escaped.md", output, {"VALUE": "unsafe"})

        self.assertFalse(output.exists())

    def test_template_entry_swap_is_rejected_by_stable_identity(self) -> None:
        module = load_cli_module()
        template_root = self.base / "template-root"
        template_root.mkdir()
        template = template_root / "stable.md"
        template.write_text("{{VALUE}}\n", encoding="utf-8")
        held = template_root / "held-original.md"
        output = self.base / "swap-output.md"

        def swap_entry(root_fd: int, name: str, template_fd: int) -> None:
            del template_fd
            os.rename(name, held.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd
            )
            try:
                os.write(descriptor, b"replacement")
            finally:
                os.close(descriptor)

        with mock.patch.object(module, "TEMPLATE_ROOT", template_root):
            with mock.patch.object(
                module, "template_read_seam", side_effect=swap_entry
            ):
                with self.assertRaisesRegex(module.WorkspaceError, "identity"):
                    module.materialize_template(
                        template.name, output, {"VALUE": "safe"}
                    )

        self.assertFalse(output.exists())

    def test_templates_resolve_from_skill_root_outside_cwd(self) -> None:
        unrelated = self.base / "unrelated-working-directory"
        unrelated.mkdir()
        init_result = self.run_cli_from(
            unrelated,
            "init",
            "--repo",
            str(self.repo),
            "--roadmap",
            str(self.roadmap),
            "--project-name",
            "cwd-independent",
        )
        root = Path(init_result.stdout.strip())
        self.run_cli_from(
            unrelated,
            "start-day",
            "--learning-root",
            str(root),
            "--day",
            "1",
        )
        stage = root / "Day01" / "进度条0-30%"
        source_result = self.run_cli_from(
            unrelated,
            "add-source",
            "--stage",
            str(stage),
            "--source",
            str(self.repo / "src" / "main.py"),
        )

        self.assertIn(
            "真实源码路径",
            (Path(source_result.stdout.strip()) / "源码导读.md").read_text(
                encoding="utf-8"
            ),
        )

    def test_missing_templates_leave_no_half_workspace_or_source(self) -> None:
        module = load_cli_module()
        missing_templates = self.base / "missing-templates"
        missing_templates.mkdir()
        final_root = self.base / "demo-project-2天学习路线"

        with mock.patch.object(module, "TEMPLATE_ROOT", missing_templates):
            with self.assertRaisesRegex(module.WorkspaceError, "template"):
                module.command_init(
                    SimpleNamespace(
                        repo=str(self.repo), roadmap=str(self.roadmap), project_name=None
                    )
                )
        self.assertFalse(final_root.exists())
        self.assert_no_temp_workspace(final_root)

        root = self.init_workspace("--project-name", "missing-source-template")
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        before_json = self.state_path(root).read_bytes()
        with mock.patch.object(module, "TEMPLATE_ROOT", missing_templates):
            with self.assertRaisesRegex(module.WorkspaceError, "template"):
                module.command_add_source(
                    SimpleNamespace(
                        stage=str(stage),
                        source=str(self.repo / "src" / "main.py"),
                        label=None,
                    )
                )
        self.assertFalse((stage / "01-main.py").exists())
        self.assertEqual(self.state_path(root).read_bytes(), before_json)

    def test_unknown_tokens_leave_no_half_workspace_or_source(self) -> None:
        module = load_cli_module()
        real_templates = SKILL_ROOT / "assets" / "templates"
        poisoned = self.base / "poisoned-templates"
        shutil.copytree(real_templates, poisoned)
        roadmap_template = poisoned / "learning-roadmap.md"
        roadmap_template.write_text(
            roadmap_template.read_text(encoding="utf-8") + "{{UNKNOWN}}\n",
            encoding="utf-8",
        )
        final_root = self.base / "demo-project-2天学习路线"
        with mock.patch.object(module, "TEMPLATE_ROOT", poisoned):
            with self.assertRaisesRegex(module.WorkspaceError, "UNKNOWN"):
                module.command_init(
                    SimpleNamespace(
                        repo=str(self.repo), roadmap=str(self.roadmap), project_name=None
                    )
                )
        self.assertFalse(final_root.exists())
        self.assert_no_temp_workspace(final_root)

        root = self.init_workspace("--project-name", "unknown-source-token")
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        source_template = poisoned / "source-guide.md"
        source_template.write_text(
            (real_templates / "source-guide.md").read_text(encoding="utf-8")
            + "{{UNKNOWN}}\n",
            encoding="utf-8",
        )
        before_json = self.state_path(root).read_bytes()
        with mock.patch.object(module, "TEMPLATE_ROOT", poisoned):
            with self.assertRaisesRegex(module.WorkspaceError, "UNKNOWN"):
                module.command_add_source(
                    SimpleNamespace(
                        stage=str(stage),
                        source=str(self.repo / "src" / "main.py"),
                        label=None,
                    )
                )
        self.assertFalse((stage / "01-main.py").exists())
        self.assertEqual(self.state_path(root).read_bytes(), before_json)

    def test_init_rejects_invalid_roadmaps_without_partial_root(self) -> None:
        cases = {
            "duplicate checkpoint": lambda data: data["days"][1]["checkpoints"].__setitem__(
                0, {"id": "d1-entry", "title": "dup", "weight": 100}
            ),
            "bad weight total": lambda data: data["days"][0]["checkpoints"][0].__setitem__(
                "weight", 19
            ),
            "boolean weight": lambda data: data["days"][0]["checkpoints"][0].__setitem__(
                "weight", True
            ),
            "float weight": lambda data: data["days"][0]["checkpoints"][0].__setitem__(
                "weight", 20.0
            ),
            "string weight": lambda data: data["days"][0]["checkpoints"][0].__setitem__(
                "weight", "20"
            ),
            "zero weight": lambda data: data["days"][0].__setitem__(
                "checkpoints",
                [
                    {"id": "d1-entry", "title": "zero", "weight": 0},
                    {"id": "d1-flow", "title": "rest", "weight": 100},
                ],
            ),
            "negative weight": lambda data: data["days"][0].__setitem__(
                "checkpoints",
                [
                    {"id": "d1-entry", "title": "negative", "weight": -10},
                    {"id": "d1-flow", "title": "rest", "weight": 110},
                ],
            ),
            "empty checkpoint id": lambda data: data["days"][0]["checkpoints"][0].__setitem__(
                "id", ""
            ),
            "gapped ranges": lambda data: data.__setitem__(
                "stage_ranges", [[0, 30], [32, 100]]
            ),
            "overlapping ranges": lambda data: data.__setitem__(
                "stage_ranges", [[0, 30], [30, 100]]
            ),
            "unordered ranges": lambda data: data.__setitem__(
                "stage_ranges", [[31, 100], [0, 30]]
            ),
            "noninteger ranges": lambda data: data.__setitem__(
                "stage_ranges", [[0, 30.5], [31, 100]]
            ),
            "wrong range endpoints": lambda data: data.__setitem__(
                "stage_ranges", [[1, 30], [31, 99]]
            ),
            "unordered days": lambda data: data["days"].reverse(),
            "missing day number": lambda data: data["days"][1].__setitem__("day", 3),
        }
        final_root = self.base / "demo-project-2天学习路线"
        for label, mutate in cases.items():
            with self.subTest(label=label):
                data = roadmap()
                mutate(data)
                self.write_roadmap(data)
                self.run_cli(
                    "init",
                    "--repo",
                    str(self.repo),
                    "--roadmap",
                    str(self.roadmap),
                    ok=False,
                )
                self.assertFalse(final_root.exists())
                self.assert_no_temp_workspace(final_root)

    def test_init_materializes_default_stage_ranges(self) -> None:
        data = roadmap()
        del data["stage_ranges"]
        self.write_roadmap(data)

        root = self.init_workspace()
        state = json.loads((root / "学习状态.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage_ranges"], [[0, 30], [31, 60], [61, 100]])

    def test_init_does_not_modify_existing_target(self) -> None:
        root = self.base / "demo-project-2天学习路线"
        root.mkdir()
        sentinel = root / "sentinel.bin"
        expected = b"do-not-touch\x00\xff"
        sentinel.write_bytes(expected)

        self.run_cli(
            "init",
            "--repo",
            str(self.repo),
            "--roadmap",
            str(self.roadmap),
            ok=False,
        )

        self.assertEqual(sentinel.read_bytes(), expected)
        self.assertEqual(sorted(root.iterdir()), [sentinel])

    def test_init_rejects_unsafe_custom_project_names_without_writes(self) -> None:
        absolute_escape = self.base / "absolute-escape"
        cases = {
            "parent traversal": "../escape",
            "absolute path": str(absolute_escape),
            "dot": ".",
            "dotdot": "..",
            "control": "bad\nname",
            "empty after sanitization": "   ",
        }
        before_entries = {path.name for path in self.base.iterdir()}
        explicit_escape_candidates = [
            self.base.parent / "escape-2天学习路线",
            Path(f"{absolute_escape}-2天学习路线"),
        ]
        for label, project_name in cases.items():
            with self.subTest(label=label):
                self.run_cli(
                    "init",
                    "--repo",
                    str(self.repo),
                    "--roadmap",
                    str(self.roadmap),
                    "--project-name",
                    project_name,
                    ok=False,
                )
                self.assertEqual({path.name for path in self.base.iterdir()}, before_entries)
                for candidate in explicit_escape_candidates:
                    self.assertFalse(candidate.exists())
                    self.assertFalse(candidate.is_symlink())
                generated_prefix = f".{self.repo.name}-2天学习路线.tmp-"
                self.assertFalse(
                    any(path.name.startswith(generated_prefix) for path in self.base.iterdir())
                )

    def test_init_accepts_safe_custom_project_name(self) -> None:
        root = self.init_workspace("--project-name", "My_Project-01")
        self.assertEqual(
            root, self.repo.resolve().parent / "My_Project-01-2天学习路线"
        )
        self.assertTrue(root.is_dir())

    def test_init_resolves_symlinked_repository_before_choosing_sibling_root(self) -> None:
        real_parent = self.base / "real-parent"
        real_repo = real_parent / "actual-project"
        real_repo.mkdir(parents=True)
        (real_repo / "main.py").write_text("print('real')\n", encoding="utf-8")
        link_parent = self.base / "link-parent"
        link_parent.mkdir()
        repo_link = link_parent / "friendly-link"
        repo_link.symlink_to(real_repo, target_is_directory=True)
        real_repo_hash = tree_hash(real_repo.resolve())

        result = self.run_cli(
            "init", "--repo", str(repo_link), "--roadmap", str(self.roadmap)
        )
        learning_root = Path(result.stdout.strip())
        expected_root = real_repo.resolve().parent / "actual-project-2天学习路线"
        state = json.loads((learning_root / "学习状态.json").read_text(encoding="utf-8"))

        self.assertEqual(learning_root, expected_root)
        self.assertFalse((link_parent / "friendly-link-2天学习路线").exists())
        self.assertEqual(Path(state["repository"]), real_repo.resolve())
        self.assertEqual(Path(state["learning_root"]), expected_root.resolve())
        self.assertEqual(tree_hash(real_repo.resolve()), real_repo_hash)

    def test_init_publish_race_does_not_clobber_new_empty_directory(self) -> None:
        module = load_cli_module()
        final_root = self.repo.resolve().parent / "demo-project-2天学习路线"
        native_publish = module._rename_no_replace

        def create_destination_then_publish(temporary_root: Path, destination: Path) -> None:
            destination.mkdir()
            native_publish(temporary_root, destination)

        with mock.patch.object(
            module, "_rename_no_replace", side_effect=create_destination_then_publish
        ):
            with self.assertRaises(module.WorkspaceError):
                module.command_init(
                    SimpleNamespace(
                        repo=str(self.repo), roadmap=str(self.roadmap), project_name=None
                    )
                )

        self.assertTrue(final_root.is_dir())
        self.assertEqual(list(final_root.iterdir()), [])
        self.assert_no_temp_workspace(final_root)

    def test_init_publish_race_does_not_clobber_new_symlink(self) -> None:
        module = load_cli_module()
        final_root = self.repo.resolve().parent / "demo-project-2天学习路线"
        symlink_target = self.base / "race-target"
        symlink_target.mkdir()
        marker = symlink_target / "sentinel.bin"
        marker.write_bytes(b"owned-by-racer")
        native_publish = module._rename_no_replace

        def create_destination_then_publish(temporary_root: Path, destination: Path) -> None:
            destination.symlink_to(symlink_target, target_is_directory=True)
            native_publish(temporary_root, destination)

        with mock.patch.object(
            module, "_rename_no_replace", side_effect=create_destination_then_publish
        ):
            with self.assertRaises(module.WorkspaceError):
                module.command_init(
                    SimpleNamespace(
                        repo=str(self.repo), roadmap=str(self.roadmap), project_name=None
                    )
                )

        self.assertTrue(final_root.is_symlink())
        self.assertEqual(final_root.readlink(), symlink_target)
        self.assertEqual(marker.read_bytes(), b"owned-by-racer")
        self.assert_no_temp_workspace(final_root)

    def test_windows_publication_uses_movefileex_without_replace_flag(self) -> None:
        module = load_cli_module()
        move = mock.Mock(return_value=1)
        kernel32 = SimpleNamespace(
            MoveFileExW=move,
            GetLastError=mock.Mock(return_value=0),
        )
        with mock.patch.object(module.sys, "platform", "win32"):
            with mock.patch.object(
                module.ctypes, "WinDLL", create=True, return_value=kernel32
            ):
                module._rename_no_replace(Path("source"), Path("destination"))

        move.assert_called_once()
        source, destination, flags = move.call_args.args
        self.assertEqual(source, "source")
        self.assertEqual(destination, "destination")
        self.assertEqual(flags, 0)

    def test_windows_publication_reports_existing_destination_without_fallback(self) -> None:
        module = load_cli_module()
        kernel32 = SimpleNamespace(
            MoveFileExW=mock.Mock(return_value=0),
            GetLastError=mock.Mock(return_value=183),
        )
        with mock.patch.object(module.sys, "platform", "win32"):
            with mock.patch.object(
                module.ctypes, "WinDLL", create=True, return_value=kernel32
            ):
                with self.assertRaisesRegex(module.WorkspaceError, "already exists"):
                    module._rename_no_replace(Path("source"), Path("destination"))

    def test_init_rejects_malformed_utf8_roadmap_without_traceback(self) -> None:
        self.roadmap.write_bytes(b'{"title":"bad\xff"}')

        result = self.run_cli(
            "init",
            "--repo",
            str(self.repo),
            "--roadmap",
            str(self.roadmap),
            ok=False,
        )

        self.assertIn("cannot read JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_init_rejects_lone_surrogate_without_traceback_or_residue(self) -> None:
        data = roadmap()
        data["title"] = "bad-\ud800-title"
        self.roadmap.write_text(json.dumps(data), encoding="utf-8")
        final_root = self.base / "demo-project-2天学习路线"

        result = self.run_cli(
            "init",
            "--repo",
            str(self.repo),
            "--roadmap",
            str(self.roadmap),
            ok=False,
        )

        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error:", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(final_root.exists())
        self.assert_no_temp_workspace(final_root)

    def test_init_uses_route_day_count_width_for_day_directories(self) -> None:
        data = roadmap()
        data["days"] = [
            {
                "day": day,
                "title": f"第 {day} 天",
                "checkpoints": [
                    {"id": f"d{day}-only", "title": "唯一检查点", "weight": 100}
                ],
            }
            for day in range(1, 101)
        ]
        self.write_roadmap(data)

        root = self.init_workspace()

        self.assertEqual(root.name, "demo-project-100天学习路线")
        self.assertTrue((root / "Day001").is_dir())
        self.assertTrue((root / "Day099").is_dir())
        self.assertTrue((root / "Day100").is_dir())
        self.assertFalse((root / "Day01").exists())

    def test_init_writes_authoritative_state_and_projection(self) -> None:
        learning_root = self.init_workspace()
        state = json.loads((learning_root / "学习状态.json").read_text(encoding="utf-8"))
        marker = json.loads(
            (learning_root / ".learning-root.json").read_text(encoding="utf-8")
        )

        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(str(uuid.UUID(state["learning_root_id"])), state["learning_root_id"])
        self.assertEqual(marker["learning_root_id"], state["learning_root_id"])
        self.assertEqual(Path(state["learning_root"]), learning_root.resolve())
        self.assertEqual(Path(state["repository"]), self.repo.resolve())
        self.assertEqual(state["route_title"], "Demo 学习路线")
        self.assertEqual(state["route_days"], 2)
        self.assertEqual(state["stage_ranges"], [[0, 30], [31, 60], [61, 100]])
        self.assertIsNone(state["current"]["day"])
        self.assertIsNone(state["current"]["stage"])
        self.assertIsNone(state["current"]["last_source"])
        self.assertEqual(state["current"]["resume_marker"], "")
        self.assertEqual(state["current"]["closed_flows"], [])
        self.assertEqual(state["stages"], [])
        self.assertEqual(state["sources"], [])
        self.assertEqual(state["artifacts"], [])
        self.assertEqual(state["history"], [])
        self.assertTrue(state["updated_at"].endswith("Z"))
        self.assertEqual(
            [
                checkpoint["status"]
                for day in state["days"]
                for checkpoint in day["checkpoints"]
            ],
            ["pending", "pending", "pending", "pending"],
        )
        projection = (learning_root / "学习状态.md").read_text(encoding="utf-8")
        self.assertIn(state["learning_root_id"], projection)
        self.assertIn(state["updated_at"], projection)

    def test_init_faults_clean_partial_temp_workspace(self) -> None:
        module = load_cli_module()
        final_root = self.base / "demo-project-2天学习路线"
        cases = {
            "json write": ("atomic_write_json", OSError("injected JSON failure")),
            "markdown write": ("atomic_write_text", OSError("injected Markdown failure")),
            "final rename": ("finalize_workspace", OSError("injected rename failure")),
        }
        for label, (seam, error) in cases.items():
            with self.subTest(label=label):
                with mock.patch.object(module, seam, side_effect=error):
                    with self.assertRaises(OSError):
                        module.command_init(
                            SimpleNamespace(
                                repo=str(self.repo),
                                roadmap=str(self.roadmap),
                                project_name=None,
                            )
                        )
                self.assertFalse(final_root.exists())
                self.assert_no_temp_workspace(final_root)
                self.assertEqual(tree_hash(self.repo), self.repo_hash)
                self.assertEqual(self.roadmap.read_bytes(), self.roadmap_bytes)

    def test_stage_range_for_maps_default_and_custom_boundaries(self) -> None:
        module = load_cli_module()
        defaults = [[0, 30], [31, 60], [61, 100]]
        self.assertEqual(module.stage_range_for(0, defaults), [0, 30])
        self.assertEqual(module.stage_range_for(30, defaults), [0, 30])
        self.assertEqual(module.stage_range_for(31, defaults), [31, 60])
        self.assertEqual(module.stage_range_for(60, defaults), [31, 60])
        self.assertEqual(module.stage_range_for(61, defaults), [61, 100])
        self.assertEqual(module.stage_range_for(100, defaults), [61, 100])

        custom = [[0, 10], [11, 99], [100, 100]]
        self.assertEqual(module.stage_range_for(10, custom), [0, 10])
        self.assertEqual(module.stage_range_for(11, custom), [11, 99])
        self.assertEqual(module.stage_range_for(99, custom), [11, 99])
        self.assertEqual(module.stage_range_for(100, custom), [100, 100])

    def test_progress_crosses_directly_from_20_to_70(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")

        state = self.read_state(root)
        self.assertEqual(state["current"]["progress"], 70)
        self.assertEqual(state["current"]["stage"]["range"], [61, 100])
        self.assertFalse((root / "Day01" / "进度条31-60%").exists())
        self.assertEqual(state["stages"][0]["status"], "sealed")
        self.assertEqual(state["stages"][-1]["status"], "active")
        self.assertEqual(active_stage_count(root), 1)

    def test_reopen_checkpoint_creates_revision_stage_and_records_reason(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")
        self.run_state_command(
            "reopen-checkpoint",
            root,
            "--checkpoint",
            "d1-flow",
            "--reason",
            "需要重新理解调用链",
        )

        self.assertTrue((root / "Day01" / "进度条0-30%-修订01").is_dir())
        state = self.read_state(root)
        self.assertEqual(state["current"]["progress"], 20)
        self.assertEqual(state["current"]["stage"]["range"], [0, 30])
        self.assertEqual(state["history"][-1]["reason"], "需要重新理解调用链")
        self.assertEqual(active_stage_count(root), 1)

    def test_second_reopen_uses_revision_02_without_touching_old_stage_trees(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("reopen-checkpoint", root, "--checkpoint", "d1-entry", "--reason", "one")
        original = root / "Day01" / "进度条0-30%"
        revision_01 = root / "Day01" / "进度条0-30%-修订01"
        old_hashes = (tree_hash(original), tree_hash(revision_01))

        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("reopen-checkpoint", root, "--checkpoint", "d1-entry", "--reason", "two")

        self.assertTrue((root / "Day01" / "进度条0-30%-修订02").is_dir())
        self.assertEqual((tree_hash(original), tree_hash(revision_01)), old_hashes)
        self.assertEqual(active_stage_count(root), 1)

    def test_recompleted_checkpoint_uses_revision_when_target_base_is_registered(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")
        base_high = root / "Day01" / "进度条61-100%"
        base_low = root / "Day01" / "进度条0-30%"
        old_hashes = (tree_hash(base_low), tree_hash(base_high))
        self.run_state_command(
            "reopen-checkpoint",
            root,
            "--checkpoint",
            "d1-flow",
            "--reason",
            "retrace",
        )
        low_revision = root / "Day01" / "进度条0-30%-修订01"
        old_low_revision_hash = tree_hash(low_revision)

        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")

        high_revision = root / "Day01" / "进度条61-100%-修订01"
        state = self.read_state(root)
        self.assertTrue(high_revision.is_dir())
        self.assertEqual(state["current"]["progress"], 70)
        self.assertEqual(state["current"]["stage"]["range"], [61, 100])
        self.assertEqual(state["current"]["stage"]["revision"], 1)
        self.assertEqual((tree_hash(base_low), tree_hash(base_high)), old_hashes)
        self.assertEqual(tree_hash(low_revision), old_low_revision_hash)

    def test_complete_day_clears_current_then_allows_next_day(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        for checkpoint in ("d1-entry", "d1-flow", "d1-summary"):
            self.run_state_command("complete-checkpoint", root, "--checkpoint", checkpoint)
        self.run_state_command("complete-day", root, "--day", "1")

        between = self.read_state(root)
        self.assertEqual(between["days"][0]["status"], "completed")
        self.assertEqual(between["stages"][-1]["status"], "sealed")
        self.assertIsNone(between["current"]["day"])
        self.assertIsNone(between["current"]["stage"])
        self.assertEqual(active_stage_count(root), 0)

        self.run_state_command("start-day", root, "--day", "2")
        state = self.read_state(root)
        self.assertEqual(state["current"]["day"], 2)
        self.assertEqual(state["days"][0]["status"], "completed")
        self.assertEqual(state["days"][1]["status"], "active")
        self.assertEqual(active_stage_count(root), 1)

    def test_invalid_state_transitions_leave_authority_unchanged(self) -> None:
        root = self.init_workspace()
        cases = [
            ("skip day", "start-day", ("--day", "2")),
        ]
        for label, command, arguments in cases:
            with self.subTest(label=label):
                before = self.state_path(root).read_bytes()
                self.run_state_command(command, root, *arguments, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before)

        self.run_state_command("start-day", root, "--day", "1")
        invalid_active_cases = [
            ("complete below 100", "complete-day", ("--day", "1")),
            (
                "complete checkpoint outside active day",
                "complete-checkpoint",
                ("--checkpoint", "d2-deps"),
            ),
            (
                "reopen checkpoint outside active day",
                "reopen-checkpoint",
                ("--checkpoint", "d2-deps", "--reason", "wrong day"),
            ),
            (
                "reopen pending checkpoint",
                "reopen-checkpoint",
                ("--checkpoint", "d1-flow", "--reason", "not completed"),
            ),
        ]
        for label, command, arguments in invalid_active_cases:
            with self.subTest(label=label):
                before = self.state_path(root).read_bytes()
                self.run_state_command(command, root, *arguments, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_two_active_stages_are_rejected_without_mutation(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        state = self.read_state(root)
        duplicate = copy.deepcopy(state["stages"][0])
        duplicate["path"] = "Day01/foreign-active"
        state["stages"].append(duplicate)
        self.state_path(root).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        before = self.state_path(root).read_bytes()

        self.run_state_command("sync-state", root, ok=False)

        self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_set_resume_rejects_non_repo_regular_files_without_json_changes(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        external = self.base / "external.py"
        external.write_text("outside\n", encoding="utf-8")
        escaping_link = self.repo / "escape.py"
        escaping_link.symlink_to(external)
        try:
            cases = {
                "external": external,
                "escaping symlink": escaping_link,
                "directory": self.repo / "src",
            }
            for label, source in cases.items():
                with self.subTest(label=label):
                    before = self.state_path(root).read_bytes()
                    self.run_state_command(
                        "set-resume", root, "--source", str(source), "--marker", "line 1", ok=False
                    )
                    self.assertEqual(self.state_path(root).read_bytes(), before)
        finally:
            escaping_link.unlink(missing_ok=True)

    def test_set_resume_stores_canonical_repo_file_and_marker(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source = self.repo / "src" / "main.py"

        self.run_state_command(
            "set-resume", root, "--source", str(source), "--marker", "run() entry"
        )

        state = self.read_state(root)
        self.assertEqual(Path(state["current"]["last_source"]), source.resolve())
        self.assertEqual(state["current"]["resume_marker"], "run() entry")

    def test_active_resume_pair_corruption_is_rejected_by_every_state_command(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        state = self.read_state(root)
        state["current"]["resume_marker"] = "entry"
        state["current"]["last_source"] = None
        self.state_path(root).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        source = self.repo / "src" / "main.py"
        commands = (
            ("start-day", ("--day", "1")),
            ("complete-checkpoint", ("--checkpoint", "d1-entry")),
            (
                "reopen-checkpoint",
                ("--checkpoint", "d1-entry", "--reason", "corrupt resume"),
            ),
            ("set-resume", ("--source", str(source), "--marker", "valid")),
            ("complete-day", ("--day", "1")),
            ("sync-state", ()),
        )
        before_tree = tree_hash(root)
        for command, arguments in commands:
            with self.subTest(command=command):
                result = self.run_state_command(command, root, *arguments, ok=False)
                self.assertIn("resume", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(tree_hash(root), before_tree)

    def test_active_resume_source_must_be_canonical_existing_repo_file(self) -> None:
        external = self.base / "external-resume-target.py"
        external.write_text("outside\n", encoding="utf-8")
        escaping_link = self.repo / "escaping-resume.py"
        escaping_link.symlink_to(external)
        cases = {
            "source without marker": (self.repo / "src" / "main.py", ""),
            "outside": (external, "outside"),
            "escaping symlink": (escaping_link, "escape"),
            "directory": (self.repo / "src", "directory"),
            "missing": (self.repo / "src" / "missing.py", "missing"),
            "noncanonical": (
                self.repo / "src" / ".." / "src" / "main.py",
                "noncanonical",
            ),
        }
        try:
            for index, (label, (source, marker)) in enumerate(cases.items(), start=1):
                with self.subTest(label=label):
                    root = self.init_workspace("--project-name", f"resume-corrupt-{index}")
                    self.run_state_command("start-day", root, "--day", "1")
                    state = self.read_state(root)
                    state["current"]["last_source"] = str(source)
                    state["current"]["resume_marker"] = marker
                    self.state_path(root).write_text(
                        json.dumps(state, ensure_ascii=False), encoding="utf-8"
                    )
                    before_tree = tree_hash(root)

                    result = self.run_state_command("sync-state", root, ok=False)

                    self.assertIn("resume", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(tree_hash(root), before_tree)
        finally:
            escaping_link.unlink(missing_ok=True)

    def test_pending_day_cannot_contain_completed_checkpoints_or_progress(self) -> None:
        root = self.init_workspace()
        state = self.read_state(root)
        day = state["days"][0]
        for checkpoint in day["checkpoints"]:
            checkpoint["status"] = "completed"
        day["progress"] = 100
        self.state_path(root).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        before_json = self.state_path(root).read_bytes()

        result = self.run_state_command("start-day", root, "--day", "1", ok=False)

        self.assertIn("pending Day", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.state_path(root).read_bytes(), before_json)

    def test_save_state_validates_candidate_before_authority_commit(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        state = module.load_state(root)
        state["history"] = {}
        before_json = self.state_path(root).read_bytes()

        with mock.patch.object(module, "replace_authority_json") as replace:
            with self.assertRaises(module.WorkspaceError):
                module.save_state(root, state)

        replace.assert_not_called()
        self.assertEqual(self.state_path(root).read_bytes(), before_json)

    def test_after_final_day_completion_start_day_fails_without_mutation(self) -> None:
        root = self.init_workspace()
        for day, checkpoints in ((1, ("d1-entry", "d1-flow", "d1-summary")), (2, ("d2-deps",))):
            self.run_state_command("start-day", root, "--day", str(day))
            for checkpoint in checkpoints:
                self.run_state_command("complete-checkpoint", root, "--checkpoint", checkpoint)
            self.run_state_command("complete-day", root, "--day", str(day))
        before = self.state_path(root).read_bytes()

        self.run_state_command("start-day", root, "--day", "2", ok=False)

        self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_precommit_failures_remove_new_stage_and_preserve_authority(self) -> None:
        module = load_cli_module()

        root = self.init_workspace()
        before = self.state_path(root).read_bytes()
        with mock.patch.object(module, "replace_authority_json", side_effect=OSError("authority fail")):
            with self.assertRaises(OSError):
                module.command_start_day(SimpleNamespace(learning_root=str(root), day=1))
        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(active_stage_count(root), 0)
        self.assertFalse((root / "Day01" / "进度条0-30%").exists())

        self.run_state_command("start-day", root, "--day", "1")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        before = self.state_path(root).read_bytes()
        with mock.patch.object(module, "replace_authority_json", side_effect=OSError("authority fail")):
            with self.assertRaises(OSError):
                module.command_complete_checkpoint(
                    SimpleNamespace(learning_root=str(root), checkpoint="d1-flow")
                )
        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(active_stage_count(root), 1)
        self.assertFalse((root / "Day01" / "进度条61-100%").exists())

        before = self.state_path(root).read_bytes()
        with mock.patch.object(module, "replace_authority_json", side_effect=OSError("authority fail")):
            with self.assertRaises(OSError):
                module.command_reopen_checkpoint(
                    SimpleNamespace(
                        learning_root=str(root), checkpoint="d1-entry", reason="retry"
                    )
                )
        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(active_stage_count(root), 1)
        self.assertFalse((root / "Day01" / "进度条0-30%-修订01").exists())

    def test_retry_cleans_only_owned_unregistered_stage_residue(self) -> None:
        root = self.init_workspace()
        state = self.read_state(root)
        target = root / "Day01" / "进度条0-30%"
        target.mkdir()
        (target / ".learning-stage.json").write_text(
            json.dumps(stage_marker(state, 1, [0, 30], 0)), encoding="utf-8"
        )
        (target / "stale.txt").write_text("stale", encoding="utf-8")

        self.run_state_command("start-day", root, "--day", "1")

        self.assertTrue(target.is_dir())
        self.assertFalse((target / "stale.txt").exists())
        self.assertEqual(active_stage_count(root), 1)

    def test_retry_rejects_and_preserves_foreign_stage_directory(self) -> None:
        root = self.init_workspace()
        target = root / "Day01" / "进度条0-30%"
        target.mkdir()
        sentinel = target / "sentinel.bin"
        sentinel.write_bytes(b"foreign")
        before = self.state_path(root).read_bytes()

        self.run_state_command("start-day", root, "--day", "1", ok=False)

        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(sentinel.read_bytes(), b"foreign")

    def test_retry_preserves_user_data_when_generated_marker_fields_are_wrong(self) -> None:
        wrong_fields = {
            "kind": "user-directory",
            "marker_version": 2,
            "schema_version": 999,
            "learning_root_id": str(uuid.uuid4()),
            "day": 2,
            "range": [31, 60],
            "revision": 1,
        }
        for index, (field, wrong_value) in enumerate(wrong_fields.items(), start=1):
            with self.subTest(field=field):
                root = self.init_workspace("--project-name", f"forged-marker-{index}")
                state = self.read_state(root)
                target = root / "Day01" / "进度条0-30%"
                target.mkdir()
                marker = stage_marker(state, 1, [0, 30], 0)
                marker[field] = wrong_value
                (target / ".learning-stage.json").write_text(
                    json.dumps(marker), encoding="utf-8"
                )
                sentinel = target / "user-data.bin"
                sentinel.write_bytes(b"must-survive")
                before_json = self.state_path(root).read_bytes()
                before_target = tree_hash(target)

                self.run_state_command("start-day", root, "--day", "1", ok=False)

                self.assertEqual(self.state_path(root).read_bytes(), before_json)
                self.assertEqual(tree_hash(target), before_target)
                self.assertEqual(sentinel.read_bytes(), b"must-survive")

    def test_checkpoint_transition_retry_cleans_owned_residue_and_rejects_foreign(self) -> None:
        owned_root = self.init_workspace("--project-name", "checkpoint-owned")
        self.run_state_command("start-day", owned_root, "--day", "1")
        self.run_state_command(
            "complete-checkpoint", owned_root, "--checkpoint", "d1-entry"
        )
        owned_state = self.read_state(owned_root)
        owned_target = owned_root / "Day01" / "进度条61-100%"
        owned_target.mkdir()
        (owned_target / ".learning-stage.json").write_text(
            json.dumps(stage_marker(owned_state, 1, [61, 100], 0)),
            encoding="utf-8",
        )
        (owned_target / "stale.txt").write_text("stale", encoding="utf-8")

        self.run_state_command(
            "complete-checkpoint", owned_root, "--checkpoint", "d1-flow"
        )

        self.assertFalse((owned_target / "stale.txt").exists())
        self.assertEqual(self.read_state(owned_root)["current"]["stage"]["range"], [61, 100])
        self.assertEqual(active_stage_count(owned_root), 1)

        foreign_root = self.init_workspace("--project-name", "checkpoint-foreign")
        self.run_state_command("start-day", foreign_root, "--day", "1")
        self.run_state_command(
            "complete-checkpoint", foreign_root, "--checkpoint", "d1-entry"
        )
        foreign_target = foreign_root / "Day01" / "进度条61-100%"
        foreign_target.mkdir()
        sentinel = foreign_target / "sentinel.bin"
        sentinel.write_bytes(b"foreign-checkpoint")
        before = self.state_path(foreign_root).read_bytes()

        self.run_state_command(
            "complete-checkpoint",
            foreign_root,
            "--checkpoint",
            "d1-flow",
            ok=False,
        )

        self.assertEqual(self.state_path(foreign_root).read_bytes(), before)
        self.assertEqual(sentinel.read_bytes(), b"foreign-checkpoint")
        self.assertEqual(active_stage_count(foreign_root), 1)

    def test_reopen_retry_cleans_owned_residue_and_rejects_foreign(self) -> None:
        owned_root = self.init_workspace("--project-name", "reopen-owned")
        self.run_state_command("start-day", owned_root, "--day", "1")
        self.run_state_command(
            "complete-checkpoint", owned_root, "--checkpoint", "d1-entry"
        )
        owned_state = self.read_state(owned_root)
        owned_target = owned_root / "Day01" / "进度条0-30%-修订01"
        owned_target.mkdir()
        (owned_target / ".learning-stage.json").write_text(
            json.dumps(stage_marker(owned_state, 1, [0, 30], 1)),
            encoding="utf-8",
        )
        (owned_target / "stale.txt").write_text("stale", encoding="utf-8")

        self.run_state_command(
            "reopen-checkpoint",
            owned_root,
            "--checkpoint",
            "d1-entry",
            "--reason",
            "review again",
        )

        self.assertFalse((owned_target / "stale.txt").exists())
        self.assertEqual(self.read_state(owned_root)["current"]["stage"]["revision"], 1)
        self.assertEqual(active_stage_count(owned_root), 1)

        foreign_root = self.init_workspace("--project-name", "reopen-foreign")
        self.run_state_command("start-day", foreign_root, "--day", "1")
        self.run_state_command(
            "complete-checkpoint", foreign_root, "--checkpoint", "d1-entry"
        )
        foreign_target = foreign_root / "Day01" / "进度条0-30%-修订01"
        foreign_target.mkdir()
        sentinel = foreign_target / "sentinel.bin"
        sentinel.write_bytes(b"foreign-reopen")
        before = self.state_path(foreign_root).read_bytes()

        self.run_state_command(
            "reopen-checkpoint",
            foreign_root,
            "--checkpoint",
            "d1-entry",
            "--reason",
            "must not clobber",
            ok=False,
        )

        self.assertEqual(self.state_path(foreign_root).read_bytes(), before)
        self.assertEqual(sentinel.read_bytes(), b"foreign-reopen")
        self.assertEqual(active_stage_count(foreign_root), 1)

    def test_active_stage_day_corruption_is_rejected_by_every_state_command(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        state = self.read_state(root)
        state["stages"][0]["day"] = 2
        state["current"]["stage"]["day"] = 2
        self.state_path(root).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        source = self.repo / "src" / "main.py"
        commands = (
            ("start-day", ("--day", "1")),
            ("complete-checkpoint", ("--checkpoint", "d1-entry")),
            (
                "reopen-checkpoint",
                ("--checkpoint", "d1-entry", "--reason", "corrupt"),
            ),
            ("set-resume", ("--source", str(source), "--marker", "entry")),
            ("complete-day", ("--day", "1")),
            ("sync-state", ()),
        )
        before_tree = tree_hash(root)
        for command, arguments in commands:
            with self.subTest(command=command):
                result = self.run_state_command(command, root, *arguments, ok=False)
                self.assertIn("active stage Day", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(tree_hash(root), before_tree)

    def test_active_stage_range_must_contain_recomputed_progress(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        state = self.read_state(root)
        state["stages"][0]["range"] = [31, 60]
        state["current"]["stage"]["range"] = [31, 60]
        self.state_path(root).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        before_tree = tree_hash(root)

        result = self.run_state_command("sync-state", root, ok=False)

        self.assertIn("active stage range", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(tree_hash(root), before_tree)

    def test_active_stage_directory_and_marker_are_validated(self) -> None:
        missing_root = self.init_workspace("--project-name", "missing-stage")
        self.run_state_command("start-day", missing_root, "--day", "1")
        missing_state = self.read_state(missing_root)
        missing_path = missing_root / missing_state["current"]["stage"]["path"]
        shutil.rmtree(missing_path)
        missing_before = tree_hash(missing_root)

        missing_result = self.run_state_command("sync-state", missing_root, ok=False)

        self.assertIn("active stage directory", missing_result.stderr)
        self.assertNotIn("Traceback", missing_result.stderr)
        self.assertEqual(tree_hash(missing_root), missing_before)

        marker_root = self.init_workspace("--project-name", "bad-stage-marker")
        self.run_state_command("start-day", marker_root, "--day", "1")
        marker_state = self.read_state(marker_root)
        marker_path = marker_root / marker_state["current"]["stage"]["path"]
        marker_data = json.loads(
            (marker_path / ".learning-stage.json").read_text(encoding="utf-8")
        )
        marker_data["learning_root_id"] = str(uuid.uuid4())
        (marker_path / ".learning-stage.json").write_text(
            json.dumps(marker_data), encoding="utf-8"
        )
        marker_before = tree_hash(marker_root)

        marker_result = self.run_state_command("sync-state", marker_root, ok=False)

        self.assertIn("active stage marker", marker_result.stderr)
        self.assertNotIn("Traceback", marker_result.stderr)
        self.assertEqual(tree_hash(marker_root), marker_before)

    def test_active_stage_path_must_match_day_range_and_revision(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        state = self.read_state(root)
        state["stages"][0]["path"] = "Day01/not-the-generated-name"
        state["current"]["stage"]["path"] = "Day01/not-the-generated-name"
        self.state_path(root).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        before_tree = tree_hash(root)

        result = self.run_state_command("sync-state", root, ok=False)

        self.assertIn("active stage path", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(tree_hash(root), before_tree)

    def test_start_day_rejects_symlinked_day_parent_without_external_writes(self) -> None:
        root = self.init_workspace()
        day_directory = root / "Day01"
        day_directory.rmdir()
        external_day = self.base / "external-day-before-start"
        external_day.mkdir()
        sentinel = external_day / "sentinel.bin"
        sentinel.write_bytes(b"outside-learning-root")
        day_directory.symlink_to(external_day, target_is_directory=True)
        before_json = self.state_path(root).read_bytes()
        before_root = tree_hash(root)
        before_external = tree_hash(external_day)

        result = self.run_state_command("start-day", root, "--day", "1", ok=False)

        self.assertIn("Day directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(tree_hash(root), before_root)
        self.assertEqual(tree_hash(external_day), before_external)
        self.assertEqual(sentinel.read_bytes(), b"outside-learning-root")

    def test_symlinked_active_day_parent_is_rejected_by_every_state_command(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        day_directory = root / "Day01"
        external_day = self.base / "external-active-day"
        shutil.move(str(day_directory), str(external_day))
        day_directory.symlink_to(external_day, target_is_directory=True)
        source = self.repo / "src" / "main.py"
        commands = (
            ("start-day", ("--day", "1")),
            ("complete-checkpoint", ("--checkpoint", "d1-entry")),
            (
                "reopen-checkpoint",
                ("--checkpoint", "d1-entry", "--reason", "corrupt parent"),
            ),
            ("set-resume", ("--source", str(source), "--marker", "entry")),
            ("complete-day", ("--day", "1")),
            ("sync-state", ()),
        )
        before_json = self.state_path(root).read_bytes()
        before_root = tree_hash(root)
        before_external = tree_hash(external_day)

        for command, arguments in commands:
            with self.subTest(command=command):
                result = self.run_state_command(command, root, *arguments, ok=False)
                self.assertIn("Day directory", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.state_path(root).read_bytes(), before_json)
                self.assertEqual(tree_hash(root), before_root)
                self.assertEqual(tree_hash(external_day), before_external)

    def test_projection_failure_keeps_committed_json_and_sync_repairs_markdown(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        before_json = self.state_path(root).read_bytes()
        with mock.patch.object(
            module, "replace_projection_markdown", side_effect=OSError("projection fail")
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "sync-state"):
                module.command_start_day(SimpleNamespace(learning_root=str(root), day=1))

        committed_json = self.state_path(root).read_bytes()
        self.assertNotEqual(committed_json, before_json)
        committed = self.read_state(root)
        stage_path = root / committed["current"]["stage"]["path"]
        self.assertTrue(stage_path.is_dir())

        module.command_sync_state(SimpleNamespace(learning_root=str(root)))

        self.assertEqual(self.state_path(root).read_bytes(), committed_json)
        self.assertEqual(
            (root / "学习状态.md").read_text(encoding="utf-8"), module.render_state(committed)
        )

    def test_sync_state_rejects_invalid_authority_without_traceback_or_mutation(self) -> None:
        mutators = {
            "malformed JSON": lambda root, state: self.state_path(root).write_text("{", encoding="utf-8"),
            "wrong schema": lambda root, state: state.__setitem__("schema_version", 999),
            "root ID mismatch": lambda root, state: state.__setitem__("learning_root_id", str(uuid.uuid4())),
            "learning root mismatch": lambda root, state: state.__setitem__("learning_root", str(self.base)),
            "repository mismatch": lambda root, state: state.__setitem__("repository", str(self.base)),
        }
        for case_index, (label, mutate) in enumerate(mutators.items(), start=1):
            with self.subTest(label=label):
                root = self.init_workspace("--project-name", f"case-{case_index}")
                state = self.read_state(root)
                mutate(root, state)
                if label != "malformed JSON":
                    self.state_path(root).write_text(
                        json.dumps(state, ensure_ascii=False), encoding="utf-8"
                    )
                before_tree = tree_hash(root)
                result = self.run_state_command("sync-state", root, ok=False)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(tree_hash(root), before_tree)

    def test_load_state_rejects_semantic_corruption_without_mutation(self) -> None:
        external = self.base / "external-resume.py"
        external.write_text("outside\n", encoding="utf-8")

        def total_101(state: dict) -> None:
            state["days"][0]["checkpoints"][0]["weight"] = 21

        def blank_checkpoint_id(state: dict) -> None:
            state["days"][0]["checkpoints"][0]["id"] = "   "

        cases = {
            "checkpoint total 101": total_101,
            "blank checkpoint ID": blank_checkpoint_id,
            "inactive progress": lambda state: state["current"].__setitem__("progress", 99),
            "inactive last source": lambda state: state["current"].__setitem__(
                "last_source", str(external)
            ),
            "history object": lambda state: state.__setitem__("history", {}),
            "sources object": lambda state: state.__setitem__("sources", {}),
            "artifacts object": lambda state: state.__setitem__("artifacts", {}),
            "closed flows object": lambda state: state["current"].__setitem__(
                "closed_flows", {}
            ),
            "resume marker list": lambda state: state["current"].__setitem__(
                "resume_marker", []
            ),
        }
        for index, (label, mutate) in enumerate(cases.items(), start=1):
            with self.subTest(label=label):
                root = self.init_workspace("--project-name", f"semantic-{index}")
                state = self.read_state(root)
                mutate(state)
                self.state_path(root).write_text(
                    json.dumps(state, ensure_ascii=False), encoding="utf-8"
                )
                before_tree = tree_hash(root)

                result = self.run_state_command("sync-state", root, ok=False)

                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(tree_hash(root), before_tree)

    def test_missing_repository_in_root_marker_is_rejected_by_all_commands(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        marker_path = root / ".learning-root.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker.pop("repository")
        marker_path.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        stage = root / "Day01" / "进度条0-30%"
        source = self.repo / "src" / "main.py"
        commands = (
            ("start-day", ("--learning-root", str(root), "--day", "1")),
            (
                "complete-checkpoint",
                ("--learning-root", str(root), "--checkpoint", "d1-entry"),
            ),
            (
                "reopen-checkpoint",
                (
                    "--learning-root",
                    str(root),
                    "--checkpoint",
                    "d1-entry",
                    "--reason",
                    "marker regression",
                ),
            ),
            (
                "set-resume",
                (
                    "--learning-root",
                    str(root),
                    "--source",
                    str(source),
                    "--marker",
                    "entry",
                ),
            ),
            (
                "add-source",
                ("--stage", str(stage), "--source", str(source)),
            ),
            ("complete-day", ("--learning-root", str(root), "--day", "1")),
            ("sync-state", ("--learning-root", str(root))),
        )
        before_tree = tree_hash(root)
        for command, arguments in commands:
            with self.subTest(command=command):
                result = self.run_cli(command, *arguments, ok=False)
                self.assertIn("repository", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(tree_hash(root), before_tree)

    def test_sync_state_restores_projection_without_changing_json(self) -> None:
        root = self.init_workspace()
        state_path = self.state_path(root)
        before_json = state_path.read_bytes()
        (root / "学习状态.md").write_text("corrupt\n", encoding="utf-8")

        self.run_state_command("sync-state", root)

        self.assertEqual(state_path.read_bytes(), before_json)
        module = load_cli_module()
        self.assertEqual(
            (root / "学习状态.md").read_text(encoding="utf-8"),
            module.render_state(self.read_state(root)),
        )

    def test_add_source_is_ordered_and_idempotent_with_json_identity(self) -> None:
        second_source = self.repo / "src" / "service.py"
        second_source.write_text("def serve():\n    return 'ok'\n", encoding="utf-8")
        self.repo_hash = tree_hash(self.repo)
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        first_source = self.repo / "src" / "main.py"

        first = self.add_source(root, first_source, stage=stage)
        again = self.add_source(root, first_source, stage=stage)
        second = self.add_source(root, second_source, stage=stage)

        first_dir = Path(first.stdout.strip())
        second_dir = Path(second.stdout.strip())
        self.assertEqual(first.stdout.strip(), again.stdout.strip())
        self.assertEqual(first_dir.name, "01-main.py")
        self.assertEqual(second_dir.name, "02-service.py")
        self.assertTrue((first_dir / "源码导读.md").is_file())
        self.assertTrue((first_dir / "产物").is_dir())
        state = self.read_state(root)
        self.assertEqual(len(state["sources"]), 2)
        self.assertEqual([entry["order"] for entry in state["sources"]], [1, 2])
        self.assertEqual(
            [entry["stage_id"] for entry in state["sources"]],
            ["Day01/进度条0-30%", "Day01/进度条0-30%"],
        )
        self.assertEqual(
            [entry["canonical_source_path"] for entry in state["sources"]],
            [str(first_source.resolve()), str(second_source.resolve())],
        )
        self.assertEqual(
            [entry["relative_learning_dir"] for entry in state["sources"]],
            ["Day01/进度条0-30%/01-main.py", "Day01/进度条0-30%/02-service.py"],
        )

    def test_same_basename_uses_stable_parent_path_suffix(self) -> None:
        tests_directory = self.repo / "tests"
        tests_directory.mkdir()
        other_main = tests_directory / "main.py"
        other_main.write_text("print('test entry')\n", encoding="utf-8")
        self.repo_hash = tree_hash(self.repo)
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")

        first = self.add_source(root, self.repo / "src" / "main.py")
        second = self.add_source(root, other_main)

        self.assertEqual(Path(first.stdout.strip()).name, "01-main.py")
        self.assertEqual(Path(second.stdout.strip()).name, "02-main-tests.py")

    def test_same_basename_parent_suffix_collision_uses_stable_path_hash(self) -> None:
        flat_parent = self.repo / "foo-bar"
        nested_parent = self.repo / "foo" / "bar"
        flat_parent.mkdir()
        nested_parent.mkdir(parents=True)
        flat_source = flat_parent / "main.py"
        nested_source = nested_parent / "main.py"
        flat_source.write_text("print('flat')\n", encoding="utf-8")
        nested_source.write_text("print('nested')\n", encoding="utf-8")
        self.repo_hash = tree_hash(self.repo)
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")

        first = self.add_source(root, self.repo / "src" / "main.py")
        second = self.add_source(root, flat_source)
        third = self.add_source(root, nested_source)

        stable = hashlib.sha256(b"foo/bar/main.py").hexdigest()[:8]
        self.assertEqual(Path(first.stdout.strip()).name, "01-main.py")
        self.assertEqual(Path(second.stdout.strip()).name, "02-main-foo-bar.py")
        self.assertEqual(
            Path(third.stdout.strip()).name,
            f"03-main-foo-bar-{stable}.py",
        )

    def test_add_source_rejects_external_escape_and_unsafe_label_without_writes(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        external = self.base / "external.py"
        external.write_text("outside\n", encoding="utf-8")
        before_json = self.state_path(root).read_bytes()
        before_stage = tree_hash(stage)
        outside_candidate = stage.parent / "escape"
        cases = (
            (external, ()),
            (self.repo / "src" / "main.py", ("--label", "../escape")),
            (self.repo / "src" / "main.py", ("--label", ".")),
            (self.repo / "src" / "main.py", ("--label", "bad/name")),
            (self.repo / "src" / "main.py", ("--label", "bad\nname")),
        )
        for source, arguments in cases:
            with self.subTest(source=source, arguments=arguments):
                self.add_source(root, source, *arguments, stage=stage, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before_json)
                self.assertEqual(tree_hash(stage), before_stage)
                self.assertFalse(outside_candidate.exists())

    def test_add_source_rejects_sealed_stage_without_mutation(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        old_stage = root / "Day01" / "进度条0-30%"
        self.add_source(root, self.repo / "src" / "main.py", stage=old_stage)
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")
        before_json = self.state_path(root).read_bytes()
        before_stage = tree_hash(old_stage)

        result = self.add_source(
            root,
            self.repo / "src" / "main.py",
            "--label",
            "again.py",
            stage=old_stage,
            ok=False,
        )

        self.assertIn("active", result.stderr)
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(tree_hash(old_stage), before_stage)

    def test_add_source_rejects_copied_or_forged_learning_root(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        copied_root = self.base / "copied-learning-root"
        shutil.copytree(root, copied_root)
        copied_stage = copied_root / "Day01" / "进度条0-30%"
        before_copy = tree_hash(copied_root)

        copied = self.add_source(
            copied_root,
            self.repo / "src" / "main.py",
            stage=copied_stage,
            ok=False,
        )

        self.assertIn("learning-root", copied.stderr)
        self.assertEqual(tree_hash(copied_root), before_copy)

        state = self.read_state(root)
        state["learning_root"] = str(copied_root.resolve())
        self.state_path(root).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        before_forged = tree_hash(root)
        forged = self.add_source(
            root,
            self.repo / "src" / "main.py",
            stage=root / "Day01" / "进度条0-30%",
            ok=False,
        )
        self.assertIn("learning-root", forged.stderr)
        self.assertEqual(tree_hash(root), before_forged)

    def test_load_state_rejects_nested_repo_and_learning_root_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as fixture_name:
            fixture = Path(fixture_name)
            roadmap_path = fixture / "roadmap.json"
            roadmap_path.write_text(
                json.dumps(roadmap(), ensure_ascii=False), encoding="utf-8"
            )

            repo_inside = fixture / "repo-inside"
            repo_inside.mkdir()
            source_inside = repo_inside / "main.py"
            source_inside.write_text("print('inside')\n", encoding="utf-8")
            root_inside = Path(
                self.run_cli(
                    "init", "--repo", str(repo_inside), "--roadmap", str(roadmap_path),
                    "--project-name", "repo-inside-case"
                ).stdout.strip()
            )
            self.run_state_command("start-day", root_inside, "--day", "1")
            moved_repo = root_inside / "nested-repo"
            shutil.move(str(repo_inside), moved_repo)
            state = self.read_state(root_inside)
            marker_path = root_inside / ".learning-root.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            state["repository"] = str(moved_repo.resolve())
            marker["repository"] = str(moved_repo.resolve())
            self.state_path(root_inside).write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            before_root = tree_hash(root_inside)
            result = self.add_source(
                root_inside,
                moved_repo / "main.py",
                stage=root_inside / "Day01" / "进度条0-30%",
                ok=False,
            )
            self.assertIn("sibling", result.stderr)
            self.assertEqual(tree_hash(root_inside), before_root)

            repo_outer = fixture / "repo-outer"
            repo_outer.mkdir()
            (repo_outer / "main.py").write_text("print('outer')\n", encoding="utf-8")
            outer_root = Path(
                self.run_cli(
                    "init", "--repo", str(repo_outer), "--roadmap", str(roadmap_path),
                    "--project-name", "learning-inside-case"
                ).stdout.strip()
            )
            self.run_state_command("start-day", outer_root, "--day", "1")
            nested_root = repo_outer / "nested-learning-root"
            shutil.move(str(outer_root), nested_root)
            state_path = nested_root / "学习状态.json"
            marker_path = nested_root / ".learning-root.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            state["learning_root"] = str(nested_root.resolve())
            marker["learning_root"] = str(nested_root.resolve())
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            before_nested = tree_hash(nested_root)
            result = self.add_source(
                nested_root,
                repo_outer / "main.py",
                stage=nested_root / "Day01" / "进度条0-30%",
                ok=False,
            )
            self.assertIn("sibling", result.stderr)
            self.assertEqual(tree_hash(nested_root), before_nested)

    def test_symlink_escapes_are_rejected_for_stage_add_source_and_resume(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        external_directory = self.base / "external-stage"
        external_directory.mkdir()
        sentinel = external_directory / "sentinel.bin"
        sentinel.write_bytes(b"outside")
        stage_link = root / "Day01" / "stage-link"
        stage_link.symlink_to(external_directory, target_is_directory=True)
        external_source = self.base / "external-source.py"
        external_source.write_text("outside\n", encoding="utf-8")
        source_link = self.repo / "src" / "escape.py"
        source_link.symlink_to(external_source)
        self.repo_hash = tree_hash(self.repo)
        before_json = self.state_path(root).read_bytes()
        before_external = tree_hash(external_directory)
        try:
            stage_result = self.add_source(
                root, self.repo / "src" / "main.py", stage=stage_link, ok=False
            )
            self.assertIn("stage", stage_result.stderr.lower())
            source_result = self.add_source(root, source_link, ok=False)
            self.assertIn("repository", source_result.stderr)
            resume_result = self.run_state_command(
                "set-resume",
                root,
                "--source",
                str(source_link),
                "--marker",
                "escape",
                ok=False,
            )
            self.assertIn("repository", resume_result.stderr)
            self.assertEqual(self.state_path(root).read_bytes(), before_json)
            self.assertEqual(tree_hash(external_directory), before_external)
            self.assertEqual(sentinel.read_bytes(), b"outside")
        finally:
            source_link.unlink(missing_ok=True)
            self.repo_hash = tree_hash(self.repo)

    def test_add_source_precommit_failure_removes_only_generated_directory(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        before_json = self.state_path(root).read_bytes()
        before_stage = tree_hash(stage)
        target = stage / "01-main.py"

        with mock.patch.object(
            module, "replace_authority_json", side_effect=OSError("authority failed")
        ):
            with self.assertRaises(OSError):
                module.command_add_source(
                    SimpleNamespace(
                        stage=str(stage), source=str(self.repo / "src" / "main.py"), label=None
                    )
                )

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(tree_hash(stage), before_stage)
        self.assertFalse(target.exists())

    def test_add_source_stage_swap_race_never_writes_through_external_symlink(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        held_stage = stage.parent / ".held-stage"
        external = self.base / "external-race-target"
        external.mkdir()
        sentinel = external / "sentinel.bin"
        sentinel.write_bytes(b"user-owned")
        before_json = self.state_path(root).read_bytes()
        before_stage = tree_hash(stage)
        before_external = tree_hash(external)

        def swap_stage(_stage: Path) -> None:
            stage.rename(held_stage)
            stage.symlink_to(external, target_is_directory=True)

        try:
            with mock.patch.object(
                module, "source_creation_seam", side_effect=swap_stage
            ):
                with self.assertRaises(module.WorkspaceError):
                    module.command_add_source(
                        SimpleNamespace(
                            stage=str(stage),
                            source=str(self.repo / "src" / "main.py"),
                            label=None,
                        )
                    )
        finally:
            if stage.is_symlink():
                stage.unlink()
            if held_stage.exists():
                held_stage.rename(stage)

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(tree_hash(stage), before_stage)
        self.assertEqual(tree_hash(external), before_external)
        self.assertEqual(sentinel.read_bytes(), b"user-owned")
        self.assertFalse(any(path.name.startswith(".01-main.py.tmp-") for path in external.iterdir()))
        self.assertFalse((external / "01-main.py").exists())

    def test_add_source_fails_closed_when_dir_fd_operations_are_unavailable(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        before_json = self.state_path(root).read_bytes()
        before_stage = tree_hash(stage)

        with mock.patch.object(module.os, "supports_dir_fd", set()):
            with self.assertRaisesRegex(module.WorkspaceError, "dir_fd"):
                module.command_add_source(
                    SimpleNamespace(
                        stage=str(stage),
                        source=str(self.repo / "src" / "main.py"),
                        label=None,
                    )
                )

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(tree_hash(stage), before_stage)

    def test_temp_cleanup_preserves_replacement_when_temp_name_identity_changes(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        moved_temp = stage / ".moved-generated-temp"
        before_json = self.state_path(root).read_bytes()
        stage_marker_before = (stage / ".learning-stage.json").read_bytes()
        replacement_name: list[str] = []

        def replace_temp(stage_fd: int, temp_name: str, temp_fd: int) -> None:
            del temp_fd
            os.rename(
                temp_name,
                moved_temp.name,
                src_dir_fd=stage_fd,
                dst_dir_fd=stage_fd,
            )
            os.mkdir(temp_name, dir_fd=stage_fd)
            replacement_name.append(temp_name)
            (stage / temp_name / "USER-DATA").write_bytes(b"preserve replacement")
            raise OSError("injected write failure after temp replacement")

        with mock.patch.object(
            module, "source_temp_creation_seam", side_effect=replace_temp
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "residue"):
                module.command_add_source(
                    SimpleNamespace(
                        stage=str(stage),
                        source=str(self.repo / "src" / "main.py"),
                        label=None,
                    )
                )

        replacement = stage / replacement_name[0]
        self.assertEqual((replacement / "USER-DATA").read_bytes(), b"preserve replacement")
        self.assertTrue(moved_temp.is_dir())
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual((stage / ".learning-stage.json").read_bytes(), stage_marker_before)
        self.assertFalse((stage / "01-main.py").exists())

    def test_precommit_cleanup_preserves_replacement_with_forged_exact_marker(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        target = stage / "01-main.py"
        moved_generated = stage / ".moved-generated-source"
        before_json = self.state_path(root).read_bytes()
        stage_marker_before = (stage / ".learning-stage.json").read_bytes()

        def replace_published_then_fail(path: Path, content: str) -> None:
            del path, content
            target.rename(moved_generated)
            target.mkdir()
            shutil.copyfile(
                moved_generated / ".learning-source.json",
                target / ".learning-source.json",
            )
            (target / "USER-DATA").write_bytes(b"preserve forged replacement")
            raise OSError("injected authority failure after replacement")

        with mock.patch.object(
            module,
            "replace_authority_json",
            side_effect=replace_published_then_fail,
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "residue"):
                module.command_add_source(
                    SimpleNamespace(
                        stage=str(stage),
                        source=str(self.repo / "src" / "main.py"),
                        label=None,
                    )
                )

        self.assertEqual((target / "USER-DATA").read_bytes(), b"preserve forged replacement")
        self.assertTrue(moved_generated.is_dir())
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual((stage / ".learning-stage.json").read_bytes(), stage_marker_before)
        self.assertEqual(self.read_state(root)["sources"], [])

    def test_add_source_retry_cleans_matching_residue_but_preserves_foreign_target(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        source = self.repo / "src" / "main.py"
        target = stage / "01-main.py"
        state = self.read_state(root)
        target.mkdir()
        (target / ".learning-source.json").write_text(
            json.dumps(source_marker(state, stage, root, source, target, 1, "main.py")),
            encoding="utf-8",
        )
        (target / "stale.bin").write_bytes(b"generated residue")

        result = self.add_source(root, source, stage=stage)

        self.assertEqual(Path(result.stdout.strip()), target)
        self.assertFalse((target / "stale.bin").exists())
        self.assertEqual(len(self.read_state(root)["sources"]), 1)

        second_root = self.init_workspace("--project-name", "foreign-source-target")
        self.run_state_command("start-day", second_root, "--day", "1")
        second_stage = second_root / "Day01" / "进度条0-30%"
        foreign = second_stage / "01-main.py"
        foreign.mkdir()
        sentinel = foreign / "sentinel.bin"
        sentinel.write_bytes(b"do-not-delete")
        forged_state = self.read_state(second_root)
        forged_marker = source_marker(
            forged_state, second_stage, second_root, source, foreign, 1, "main.py"
        )
        forged_marker["learning_root_id"] = str(uuid.uuid4())
        (foreign / ".learning-source.json").write_text(
            json.dumps(forged_marker), encoding="utf-8"
        )
        before_json = self.state_path(second_root).read_bytes()

        failed = self.add_source(second_root, source, stage=second_stage, ok=False)

        self.assertIn("unknown", failed.stderr)
        self.assertEqual(sentinel.read_bytes(), b"do-not-delete")
        self.assertEqual(self.state_path(second_root).read_bytes(), before_json)

    def test_same_source_in_later_stage_gets_new_order_without_mutating_sealed_stage(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source = self.repo / "src" / "main.py"
        old_stage = root / "Day01" / "进度条0-30%"
        old_source_dir = Path(self.add_source(root, source, stage=old_stage).stdout.strip())
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")
        old_hash = tree_hash(old_source_dir)
        new_stage = root / "Day01" / "进度条61-100%"

        new_source_dir = Path(
            self.add_source(root, source, stage=new_stage).stdout.strip()
        )

        self.assertEqual(new_source_dir.name, "01-main.py")
        self.assertNotEqual(new_source_dir, old_source_dir)
        self.assertEqual(tree_hash(old_source_dir), old_hash)
        registrations = self.read_state(root)["sources"]
        self.assertEqual(len(registrations), 2)
        self.assertEqual(
            {entry["stage_id"] for entry in registrations},
            {"Day01/进度条0-30%", "Day01/进度条61-100%"},
        )

    def test_forged_source_records_are_rejected_without_projection_writes(self) -> None:
        mutators = {
            "duplicate identity": lambda state: state["sources"].append(
                copy.deepcopy(state["sources"][0])
            ),
            "unknown stage": lambda state: state["sources"][0].__setitem__(
                "stage_id", "Day01/unknown-stage"
            ),
            "external canonical source": lambda state: state["sources"][0].__setitem__(
                "canonical_source_path", str((self.base / "external.py").resolve())
            ),
            "escaping learning directory": lambda state: state["sources"][0].__setitem__(
                "relative_learning_dir", "../escape"
            ),
            "missing creation timestamp": lambda state: state["sources"][0].pop(
                "created_at"
            ),
        }
        (self.base / "external.py").write_text("outside\n", encoding="utf-8")
        for index, (label, mutate) in enumerate(mutators.items(), start=1):
            with self.subTest(label=label):
                root = self.init_workspace("--project-name", f"source-forgery-{index}")
                self.run_state_command("start-day", root, "--day", "1")
                self.add_source(root, self.repo / "src" / "main.py")
                state = self.read_state(root)
                mutate(state)
                self.state_path(root).write_text(
                    json.dumps(state, ensure_ascii=False), encoding="utf-8"
                )
                before_tree = tree_hash(root)

                result = self.run_state_command("sync-state", root, ok=False)

                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(tree_hash(root), before_tree)

    def test_sealed_stage_corruption_is_rejected_by_every_state_command(self) -> None:
        source = self.repo / "src" / "main.py"
        corruption_kinds = ("missing", "symlink", "forged marker")
        for index, corruption in enumerate(corruption_kinds, start=1):
            with self.subTest(corruption=corruption):
                root = self.init_workspace("--project-name", f"sealed-corrupt-{index}")
                self.run_state_command("start-day", root, "--day", "1")
                self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
                self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")
                sealed = root / "Day01" / "进度条0-30%"
                external = self.base / f"sealed-external-{index}"
                external_hash = None
                if corruption == "missing":
                    shutil.rmtree(sealed)
                elif corruption == "symlink":
                    shutil.move(str(sealed), str(external))
                    sealed.symlink_to(external, target_is_directory=True)
                    external_hash = tree_hash(external)
                else:
                    marker_path = sealed / ".learning-stage.json"
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    marker["learning_root_id"] = str(uuid.uuid4())
                    marker_path.write_text(json.dumps(marker), encoding="utf-8")
                before_tree = tree_hash(root)
                commands = (
                    ("sync-state", ()),
                    ("start-day", ("--day", "1")),
                    ("complete-checkpoint", ("--checkpoint", "d1-summary")),
                    (
                        "reopen-checkpoint",
                        ("--checkpoint", "d1-entry", "--reason", "corrupt sealed stage"),
                    ),
                    ("set-resume", ("--source", str(source), "--marker", "entry")),
                    ("complete-day", ("--day", "1")),
                    (
                        "add-source",
                        (
                            "--stage",
                            str(root / "Day01" / "进度条61-100%"),
                            "--source",
                            str(source),
                        ),
                    ),
                )
                for command, arguments in commands:
                    with self.subTest(corruption=corruption, command=command):
                        if command == "add-source":
                            result = self.run_cli(command, *arguments, ok=False)
                        else:
                            result = self.run_state_command(
                                command, root, *arguments, ok=False
                            )
                        self.assertIn("stage", result.stderr.lower())
                        self.assertNotIn("Traceback", result.stderr)
                        self.assertEqual(tree_hash(root), before_tree)
                        if external_hash is not None:
                            self.assertEqual(tree_hash(external), external_hash)

    def test_add_source_help_exits_zero(self) -> None:
        result = self.run_cli("add-source", "--help")
        self.assertEqual(result.returncode, 0)

    def test_implemented_command_help_exits_zero(self) -> None:
        root_help = self.run_cli("--help")
        self.assertEqual(root_help.returncode, 0)
        for command in (
            "init",
            "start-day",
            "complete-checkpoint",
            "reopen-checkpoint",
            "set-resume",
            "complete-day",
            "sync-state",
        ):
            with self.subTest(command=command):
                result = self.run_cli(command, "--help")
                self.assertEqual(result.returncode, 0)

    def test_register_source_artifact_records_and_projects_idempotently(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "entry-sequence.html"
        artifact.write_text("<html>source flow</html>\n", encoding="utf-8")

        first = self.register_artifact(root, "source", source_dir, artifact)
        self.assertEqual(Path(first.stdout.strip()), artifact.resolve())
        first_state = self.read_state(root)
        self.assertEqual(len(first_state["artifacts"]), 1)
        record = first_state["artifacts"][0]
        self.assertEqual(record["canonical_artifact_path"], str(artifact.resolve()))
        self.assertEqual(record["owner_id"], f"source:{source_dir.relative_to(root).as_posix()}")
        self.assertEqual(record["owner_kind"], "source")
        self.assertEqual(record["relative_owner_dir"], source_dir.relative_to(root).as_posix())
        self.assertEqual(record["relative_artifact_path"], "产物/entry-sequence.html")
        guide = source_dir / "源码导读.md"
        projected = guide.read_text(encoding="utf-8")
        self.assertIn("`产物/entry-sequence.html`", projected)
        self.assertEqual(projected.count("产物/entry-sequence.html"), 1)

        self.register_artifact(root, "source", source_dir, artifact)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)
        self.assertEqual(
            (source_dir / "源码导读.md").read_text(encoding="utf-8").count(
                "产物/entry-sequence.html"
            ),
            1,
        )

    def test_register_day_overview_artifact_records_in_overview_markdown(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "进度条0-30%" / "当日总览产物"
        artifact = overview / "day-framework.svg"
        artifact.write_text("<svg/>\n", encoding="utf-8")

        self.register_artifact(root, "day-overview", overview, artifact)

        state = self.read_state(root)
        self.assertEqual(len(state["artifacts"]), 1)
        self.assertEqual(
            state["artifacts"][0]["owner_id"], "day-overview:Day01/进度条0-30%"
        )
        summary = (overview / "当日统一总结.md").read_text(encoding="utf-8")
        self.assertIn("`day-framework.svg`", summary)

    def test_register_artifact_rejects_unsafe_owner_and_artifact_inputs(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact_dir = source_dir / "产物"
        valid = artifact_dir / "valid.html"
        valid.write_text("ok\n", encoding="utf-8")
        outside = self.base / "outside.html"
        outside.write_text("outside\n", encoding="utf-8")
        directory = artifact_dir / "not-a-file"
        directory.mkdir()
        artifact_link = artifact_dir / "escape.html"
        artifact_link.symlink_to(outside)
        owner_link = self.base / "owner-link"
        owner_link.symlink_to(source_dir, target_is_directory=True)
        unregistered = root / "Day01" / "进度条0-30%" / "99-unregistered"
        unregistered.mkdir()
        before = self.state_path(root).read_bytes()

        cases = (
            ("source", owner_link, valid),
            ("source", source_dir / ".." / source_dir.name, valid),
            ("source", source_dir, outside),
            ("source", source_dir, artifact_link),
            ("source", source_dir, directory),
            ("source", unregistered, valid),
            ("day-overview", source_dir, valid),
        )
        for owner_kind, owner, artifact in cases:
            with self.subTest(owner_kind=owner_kind, owner=owner, artifact=artifact):
                self.register_artifact(root, owner_kind, owner, artifact, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_register_artifact_rejects_sealed_source_and_day_owner(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py", stage=stage).stdout.strip()
        )
        source_artifact = source_dir / "产物" / "sealed.html"
        source_artifact.write_text("sealed\n", encoding="utf-8")
        overview = stage / "当日总览产物"
        day_artifact = overview / "sealed.svg"
        day_artifact.write_text("sealed\n", encoding="utf-8")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")

        before = self.state_path(root).read_bytes()
        self.register_artifact(root, "source", source_dir, source_artifact, ok=False)
        self.register_artifact(root, "day-overview", overview, day_artifact, ok=False)
        self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_register_artifact_requires_exactly_one_managed_block(self) -> None:
        module = load_cli_module()
        for markdown in (
            "# no block\n",
            "<!-- learning-coach:artifacts:start -->\nfirst\n"
            "<!-- learning-coach:artifacts:start -->\nsecond\n"
            "<!-- learning-coach:artifacts:end -->\n",
            "<!-- learning-coach:artifacts:end -->\n"
            "<!-- learning-coach:artifacts:start -->\n",
        ):
            with self.subTest(markdown=markdown):
                with self.assertRaises(module.WorkspaceError):
                    module.replace_managed_artifact_block(markdown, ["- `a.html`"])

        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "diagram.html"
        artifact.write_text("diagram\n", encoding="utf-8")
        guide = source_dir / "源码导读.md"
        guide.write_text("human text without markers\n", encoding="utf-8")
        before_json = self.state_path(root).read_bytes()
        before_guide = guide.read_bytes()

        self.register_artifact(root, "source", source_dir, artifact, ok=False)

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(guide.read_bytes(), before_guide)

    def test_register_artifact_transaction_and_retry_repair_projection(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "sequence.html"
        artifact.write_text("sequence\n", encoding="utf-8")
        guide = source_dir / "源码导读.md"
        original = guide.read_text(encoding="utf-8")
        customized = original.replace(
            "- 当前理解：待整理", "- 当前理解：人工内容，必须逐字保留"
        )
        with guide.open("w", encoding="utf-8", newline="") as handle:
            handle.write(customized)
        before_json = self.state_path(root).read_bytes()
        before_guide = guide.read_bytes()
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="source",
            owner_dir=str(source_dir),
            artifact=str(artifact),
        )

        with mock.patch.object(
            module, "artifact_pre_authority_seam", side_effect=OSError("pre commit")
        ):
            with self.assertRaises(OSError):
                module.command_register_artifact(namespace)
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(guide.read_bytes(), before_guide)
        self.assertEqual(self.read_state(root)["artifacts"], [])

        with mock.patch.object(
            module, "replace_authority_json", side_effect=OSError("JSON commit failed")
        ):
            with self.assertRaises(OSError):
                module.command_register_artifact(namespace)
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(guide.read_bytes(), before_guide)
        self.assertEqual(self.read_state(root)["artifacts"], [])

        with mock.patch.object(
            module, "replace_owner_markdown", side_effect=OSError("projection failed")
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "authoritative JSON.*retry"):
                module.command_register_artifact(namespace)

        committed = self.read_state(root)
        self.assertEqual(len(committed["artifacts"]), 1)
        self.assertEqual(guide.read_bytes(), before_guide)

        module.command_register_artifact(namespace)
        repaired = guide.read_text(encoding="utf-8")
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)
        self.assertIn("人工内容，必须逐字保留", repaired)
        self.assertEqual(repaired.count("sequence.html"), 1)
        prefix_before = customized.split("<!-- learning-coach:artifacts:start -->", 1)[0]
        prefix_after = repaired.split("<!-- learning-coach:artifacts:start -->", 1)[0]
        suffix_before = customized.split("<!-- learning-coach:artifacts:end -->", 1)[1]
        suffix_after = repaired.split("<!-- learning-coach:artifacts:end -->", 1)[1]
        self.assertEqual(prefix_after, prefix_before)
        self.assertEqual(suffix_after, suffix_before)

    def test_register_artifact_validation_rejects_duplicate_or_wrong_owner(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "flow.html"
        artifact.write_text("flow\n", encoding="utf-8")
        self.register_artifact(root, "source", source_dir, artifact)
        state = self.read_state(root)
        duplicate = copy.deepcopy(state["artifacts"][0])
        duplicate["owner_id"] = "day-overview:Day01/进度条0-30%"
        duplicate["owner_kind"] = "day-overview"
        duplicate["relative_owner_dir"] = "Day01/进度条0-30%/当日总览产物"
        state["artifacts"].append(duplicate)
        self.state_path(root).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        before_tree = tree_hash(root)

        failed = self.run_state_command("sync-state", root, ok=False)

        self.assertIn("artifact", failed.stderr.lower())
        self.assertEqual(tree_hash(root), before_tree)

    def test_register_artifact_races_fail_closed_without_wrong_file_overwrite(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "race.html"
        artifact.write_text("race\n", encoding="utf-8")
        guide = source_dir / "源码导读.md"
        original_guide = guide.read_bytes()
        outside = self.base / "outside-guide.md"
        outside.write_bytes(b"outside sentinel")
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="source",
            owner_dir=str(source_dir),
            artifact=str(artifact),
        )

        def replace_guide_with_symlink(*_args) -> None:
            guide.unlink()
            guide.symlink_to(outside)

        with mock.patch.object(
            module,
            "artifact_pre_authority_seam",
            side_effect=replace_guide_with_symlink,
        ):
            with self.assertRaises(module.WorkspaceError):
                module.command_register_artifact(namespace)

        self.assertEqual(outside.read_bytes(), b"outside sentinel")
        self.assertEqual(self.read_state(root)["artifacts"], [])
        guide.unlink()
        guide.write_bytes(original_guide)

    def test_register_artifact_owner_and_file_swap_races_fail_closed(self) -> None:
        module = load_cli_module()
        for race_kind in ("artifact", "owner"):
            with self.subTest(race_kind=race_kind):
                root = self.init_workspace(
                    "--project-name", f"artifact-race-{race_kind}"
                )
                self.run_state_command("start-day", root, "--day", "1")
                source_dir = Path(
                    self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
                )
                artifact = source_dir / "产物" / "race.html"
                artifact.write_text("original artifact\n", encoding="utf-8")
                moved_owner = source_dir.with_name(source_dir.name + "-moved")
                external_owner = self.base / f"external-owner-{race_kind}"
                external_owner.mkdir()
                (external_owner / "源码导读.md").write_bytes(b"outside guide sentinel")
                (external_owner / "产物").mkdir()
                namespace = SimpleNamespace(
                    learning_root=str(root),
                    owner_kind="source",
                    owner_dir=str(source_dir),
                    artifact=str(artifact),
                )

                def swap_after_prepare(*_args) -> None:
                    if race_kind == "artifact":
                        artifact.unlink()
                        artifact.write_text("replacement artifact\n", encoding="utf-8")
                    else:
                        source_dir.rename(moved_owner)
                        source_dir.symlink_to(external_owner, target_is_directory=True)

                with mock.patch.object(
                    module,
                    "artifact_pre_authority_seam",
                    side_effect=swap_after_prepare,
                ):
                    with self.assertRaises(module.WorkspaceError):
                        module.command_register_artifact(namespace)

                raw_state = json.loads(self.state_path(root).read_text(encoding="utf-8"))
                self.assertEqual(raw_state["artifacts"], [])
                self.assertEqual(
                    (external_owner / "源码导读.md").read_bytes(),
                    b"outside guide sentinel",
                )
                if race_kind == "artifact":
                    self.assertEqual(
                        artifact.read_text(encoding="utf-8"), "replacement artifact\n"
                    )

    def test_registered_artifacts_remain_valid_after_owner_stage_is_sealed(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "durable.html"
        artifact.write_text("durable\n", encoding="utf-8")
        self.register_artifact(root, "source", source_dir, artifact)

        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")
        self.run_state_command("sync-state", root)

        state = self.read_state(root)
        self.assertEqual(len(state["artifacts"]), 1)
        self.assertEqual(state["stages"][0]["status"], "sealed")

    def test_register_artifact_fails_closed_without_dir_fd_primitives(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "unsupported.html"
        artifact.write_text("unsupported\n", encoding="utf-8")
        before_json = self.state_path(root).read_bytes()

        with mock.patch.object(module.os, "supports_dir_fd", set()):
            with self.assertRaisesRegex(module.WorkspaceError, "directory-fd"):
                module.command_register_artifact(
                    SimpleNamespace(
                        learning_root=str(root),
                        owner_kind="source",
                        owner_dir=str(source_dir),
                        artifact=str(artifact),
                    )
                )

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertNotIn(
            "unsupported.html",
            (source_dir / "源码导读.md").read_text(encoding="utf-8"),
        )

    def test_register_artifact_never_overwrites_concurrent_in_place_human_edits(self) -> None:
        module = load_cli_module()
        for phase in ("pre-authority", "post-authority"):
            with self.subTest(phase=phase):
                root = self.init_workspace(
                    "--project-name", f"in-place-edit-{phase}"
                )
                self.run_state_command("start-day", root, "--day", "1")
                source_dir = Path(
                    self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
                )
                artifact = source_dir / "产物" / "concurrent.html"
                artifact.write_text("artifact\n", encoding="utf-8")
                guide = source_dir / "源码导读.md"
                before_json = self.state_path(root).read_bytes()
                human_append = f"\nHUMAN APPEND {phase}\n".encode("utf-8")
                namespace = SimpleNamespace(
                    learning_root=str(root),
                    owner_kind="source",
                    owner_dir=str(source_dir),
                    artifact=str(artifact),
                )

                def append_in_place(*_args) -> None:
                    with guide.open("ab") as handle:
                        handle.write(human_append)

                seam = (
                    "artifact_pre_authority_seam"
                    if phase == "pre-authority"
                    else "artifact_post_authority_seam"
                )
                with mock.patch.object(module, seam, side_effect=append_in_place):
                    with self.assertRaises(module.WorkspaceError):
                        module.command_register_artifact(namespace)

                self.assertTrue(guide.read_bytes().endswith(human_append))
                if phase == "pre-authority":
                    self.assertEqual(self.state_path(root).read_bytes(), before_json)
                    self.assertEqual(self.read_state(root)["artifacts"], [])
                else:
                    self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

                module.command_register_artifact(namespace)
                repaired = guide.read_bytes()
                self.assertIn(human_append, repaired)
                self.assertEqual(repaired.count(b"concurrent.html"), 1)
                self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

    def test_managed_artifact_block_requires_isolated_balanced_marker_lines(self) -> None:
        module = load_cli_module()
        malformed = (
            "prefix <!-- learning-coach:artifacts:start -->\n"
            "<!-- learning-coach:artifacts:end -->\n",
            "<!-- learning-coach:artifacts:start --> trailing\n"
            "<!-- learning-coach:artifacts:end -->\n",
            "<!-- learning-coach:artifacts:start -->\r"
            "<!-- learning-coach:artifacts:end -->\r",
            "<!-- learning-coach:artifacts:end -->\n"
            "<!-- learning-coach:artifacts:start -->\n",
            "<!-- learning-coach:artifacts:start -->\n"
            "<!-- learning-coach:artifacts:start -->\n"
            "<!-- learning-coach:artifacts:end -->\n",
            "<!-- learning-coach:artifacts:start -->\n"
            "<!-- learning-coach:artifacts:end -->\n"
            "inline <!-- learning-coach:artifacts:end -->\n",
        )
        for markdown in malformed:
            with self.subTest(markdown=repr(markdown)):
                with self.assertRaises(module.WorkspaceError):
                    module.replace_managed_artifact_block(markdown, ["- `safe.html`"])

        crlf = (
            "human-before\r\n"
            "<!-- learning-coach:artifacts:start -->\r\n"
            "old\r\n"
            "<!-- learning-coach:artifacts:end -->\r\n"
            "human-after\r\n"
        )
        replaced = module.replace_managed_artifact_block(crlf, ["- `safe.html`"])
        self.assertTrue(replaced.startswith("human-before\r\n"))
        self.assertTrue(replaced.endswith("\r\nhuman-after\r\n"))
        self.assertIn("- `safe.html`\r\n", replaced)

    def test_register_artifact_rejects_control_and_marker_injection_names(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        guide = source_dir / "源码导读.md"
        before_json = self.state_path(root).read_bytes()
        before_guide = guide.read_bytes()
        bad_names = (
            "bad\nname.html",
            "bad<!-- learning-coach:artifacts:end -->.html",
            "bad<!-- learning-coach:artifacts:start -->.html",
        )
        for name in bad_names:
            with self.subTest(name=repr(name)):
                artifact = source_dir / "产物" / name
                artifact.write_text("bad name\n", encoding="utf-8")
                self.register_artifact(root, "source", source_dir, artifact, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before_json)
                self.assertEqual(guide.read_bytes(), before_guide)

    def test_projection_retry_is_allowed_after_owner_stage_becomes_sealed(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py", stage=stage).stdout.strip()
        )
        overview = stage / "当日总览产物"
        registrations = (
            (
                SimpleNamespace(
                    learning_root=str(root),
                    owner_kind="source",
                    owner_dir=str(source_dir),
                    artifact=str(source_dir / "产物" / "source-repair.html"),
                ),
                source_dir / "源码导读.md",
                "source-repair.html",
            ),
            (
                SimpleNamespace(
                    learning_root=str(root),
                    owner_kind="day-overview",
                    owner_dir=str(overview),
                    artifact=str(overview / "day-repair.svg"),
                ),
                overview / "当日统一总结.md",
                "day-repair.svg",
            ),
        )
        for namespace, _markdown, _name in registrations:
            Path(namespace.artifact).write_text("artifact\n", encoding="utf-8")
            with mock.patch.object(
                module, "replace_owner_markdown", side_effect=OSError("projection")
            ):
                with self.assertRaisesRegex(module.WorkspaceError, "authoritative JSON"):
                    module.command_register_artifact(namespace)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 2)

        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")
        self.assertEqual(self.read_state(root)["stages"][0]["status"], "sealed")

        for namespace, markdown, name in registrations:
            with markdown.open("ab") as handle:
                handle.write(b"\nHUMAN AFTER SEAL\n")
            module.command_register_artifact(namespace)
            projected = markdown.read_bytes()
            self.assertIn(b"HUMAN AFTER SEAL", projected)
            self.assertEqual(projected.count(name.encode("utf-8")), 1)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 2)

    def test_artifact_physical_identity_is_unique_and_replacement_is_rejected(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        stage = root / "Day01" / "进度条0-30%"
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py", stage=stage).stdout.strip()
        )
        original = source_dir / "产物" / "CaseFlow.HTML"
        alias = source_dir / "产物" / "caseflow.html"
        original.write_text("same physical file\n", encoding="utf-8")
        if not alias.exists():
            os.link(original, alias)
        self.assertTrue(os.path.samefile(original, alias))

        self.register_artifact(root, "source", source_dir, original)
        self.register_artifact(root, "source", source_dir, alias)

        state = self.read_state(root)
        self.assertEqual(len(state["artifacts"]), 1)
        record = state["artifacts"][0]
        metadata = original.stat()
        self.assertEqual(record["artifact_device"], metadata.st_dev)
        self.assertEqual(record["artifact_inode"], metadata.st_ino)
        overview = stage / "当日总览产物"
        second_owner_alias = overview / "same-physical.html"
        os.link(original, second_owner_alias)
        self.register_artifact(
            root, "day-overview", overview, second_owner_alias, ok=False
        )
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

        original.unlink()
        original.write_text("replacement file\n", encoding="utf-8")
        before_tree = tree_hash(root)
        failed = self.run_state_command("sync-state", root, ok=False)
        self.assertIn("artifact", failed.stderr.lower())
        self.assertEqual(tree_hash(root), before_tree)

    def test_artifact_relative_path_must_be_exact_canonical_lexical_form(self) -> None:
        corruptions = (
            "产物/./flow.html",
            "产物//flow.html",
            "产物\\flow.html",
        )
        for index, corrupted in enumerate(corruptions, start=1):
            with self.subTest(corrupted=corrupted):
                root = self.init_workspace(
                    "--project-name", f"artifact-relative-{index}"
                )
                self.run_state_command("start-day", root, "--day", "1")
                source_dir = Path(
                    self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
                )
                artifact = source_dir / "产物" / "flow.html"
                artifact.write_text("flow\n", encoding="utf-8")
                self.register_artifact(root, "source", source_dir, artifact)
                state = self.read_state(root)
                state["artifacts"][0]["relative_artifact_path"] = corrupted
                self.state_path(root).write_text(
                    json.dumps(state, ensure_ascii=False), encoding="utf-8"
                )
                before_tree = tree_hash(root)

                failed = self.run_state_command("sync-state", root, ok=False)

                self.assertIn("artifact", failed.stderr.lower())
                self.assertEqual(tree_hash(root), before_tree)

    def test_projection_exchange_preserves_in_place_edit_at_final_publish_seam(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "exchange-race.html"
        artifact.write_text("artifact\n", encoding="utf-8")
        guide = source_dir / "源码导读.md"
        human_append = b"\nHUMAN EDIT AT EXCHANGE\n"
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="source",
            owner_dir=str(source_dir),
            artifact=str(artifact),
        )

        def append_at_exchange(*_args) -> None:
            with guide.open("ab") as handle:
                handle.write(human_append)

        with mock.patch.object(
            module,
            "owner_projection_exchange_seam",
            side_effect=append_at_exchange,
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "authoritative JSON.*retry"):
                module.command_register_artifact(namespace)

        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)
        failed_projection = guide.read_bytes()
        self.assertTrue(failed_projection.endswith(human_append))
        self.assertNotIn(b"exchange-race.html", failed_projection)

        module.command_register_artifact(namespace)

        repaired = guide.read_bytes()
        self.assertIn(human_append, repaired)
        self.assertEqual(repaired.count(b"exchange-race.html"), 1)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

    def test_projection_exchange_never_publishes_replaced_temp_entry(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        source_dir = Path(
            self.add_source(root, self.repo / "src" / "main.py").stdout.strip()
        )
        artifact = source_dir / "产物" / "safe-candidate.html"
        artifact.write_text("artifact\n", encoding="utf-8")
        guide = source_dir / "源码导读.md"
        original_guide = guide.read_bytes()
        attacker_bytes = b"ATTACKER TEMP CONTENT\n"
        observed_temp_names: list[str] = []
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="source",
            owner_dir=str(source_dir),
            artifact=str(artifact),
        )

        def replace_temp_entry(
            directory_fd: int,
            _owner_name: str,
            temporary_name: str,
            _original_fd: int,
            _candidate_fd: int,
        ) -> None:
            observed_temp_names.append(temporary_name)
            os.unlink(temporary_name, dir_fd=directory_fd)
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(descriptor, attacker_bytes)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        with mock.patch.object(
            module,
            "owner_projection_exchange_seam",
            side_effect=replace_temp_entry,
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "authoritative JSON.*retry"):
                module.command_register_artifact(namespace)

        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)
        self.assertEqual(guide.read_bytes(), original_guide)
        self.assertNotIn(attacker_bytes, guide.read_bytes())
        self.assertEqual(len(observed_temp_names), 1)
        foreign_temp = source_dir / observed_temp_names[0]
        self.assertEqual(foreign_temp.read_bytes(), attacker_bytes)

        module.command_register_artifact(namespace)

        self.assertEqual(
            (source_dir / "源码导读.md").read_bytes().count(b"safe-candidate.html"),
            1,
        )
        self.assertEqual(foreign_temp.read_bytes(), attacker_bytes)

    def test_register_artifact_help_and_root_help_include_final_command(self) -> None:
        self.assertEqual(self.run_cli("register-artifact", "--help").returncode, 0)
        root_help = self.run_cli("--help")
        self.assertIn("register-artifact", root_help.stdout)
        for command in (
            "init",
            "start-day",
            "complete-checkpoint",
            "reopen-checkpoint",
            "set-resume",
            "add-source",
            "register-artifact",
            "complete-day",
            "sync-state",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command, "--help").returncode, 0)
