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


def active_chain_count(root: Path) -> int:
    state = json.loads((root / "学习状态.json").read_text(encoding="utf-8"))
    return sum(chain["status"] == "active" for chain in state["chains"])


def chain_marker(state: dict, day: int, order: int, name: str) -> dict:
    return {
        "kind": "source-code-learning-chain",
        "marker_version": 1,
        "schema_version": 3,
        "learning_root_id": state["learning_root_id"],
        "day": day,
        "order": order,
        "name": name,
    }


def roadmap() -> dict:
    return {
        "title": "Demo 学习路线",
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

    def begin_chain(self, root: Path, day: int, name: str, ok: bool = True):
        return self.run_state_command(
            "begin-chain", root, "--day", str(day), "--name", name, ok=ok
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

    def test_init_creates_oral_answer_archive_for_every_day(self) -> None:
        root = self.init_workspace()
        expected_days = (
            (root / "Day01" / "口述回答归档.md", "# Day 1｜入口"),
            (root / "Day02" / "口述回答归档.md", "# Day 2｜依赖"),
        )
        for archive_path, title in expected_days:
            with self.subTest(day=archive_path.parent.name):
                self.assertTrue(archive_path.is_file())
                content = archive_path.read_text(encoding="utf-8")
                self.assertNotIn("{{", content)
                self.assertNotIn("}}", content)
                self.assertEqual(content.splitlines()[0], f"{title}｜口述回答归档")
                self.assertIn("题干", content)
                self.assertIn("代码执行调用链", content)
                self.assertIn("口述答案", content)
                self.assertIn("仅在学员明确同意归档后追加记录", content)

    def test_templates_project_durable_learning_content(self) -> None:
        root = self.init_workspace()
        self.assertIn(
            "## Day 1", (root / "学习路线.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "当前进度", (root / "学习状态.md").read_text(encoding="utf-8")
        )

        self.run_state_command("start-day", root, "--day", "1")
        day_dir = root / "Day01"
        chain_dir = Path(self.begin_chain(root, 1, "入口分析").stdout.strip())

        brief = (chain_dir / "链说明.md").read_text(encoding="utf-8")
        self.assertIn("源码入口", brief)
        self.assertIn("本链调用链树", brief)
        self.assertNotIn("{{", brief)
        self.assertIn(
            "用户问题", (day_dir / "问题与解答.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "已闭环流程", (day_dir / "当日总结.md").read_text(encoding="utf-8")
        )
        overview = day_dir / "当日总览产物" / "当日统一总结.md"
        overview_text = overview.read_text(encoding="utf-8")
        self.assertIn("整体框架联系", overview_text)
        self.assertEqual(
            overview_text.count("<!-- learning-coach:artifacts:start -->"), 1
        )
        self.assertEqual(
            overview_text.count("<!-- learning-coach:artifacts:end -->"), 1
        )
        self.assertNotIn("<!-- learning-coach:artifacts:start -->", brief)

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
        chain_result = self.run_cli_from(
            unrelated,
            "begin-chain",
            "--learning-root",
            str(root),
            "--day",
            "1",
            "--name",
            "入口分析",
        )

        self.assertIn(
            "源码入口",
            (Path(chain_result.stdout.strip()) / "链说明.md").read_text(
                encoding="utf-8"
            ),
        )

    def test_missing_templates_leave_no_half_workspace_or_chain(self) -> None:
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

        root = self.init_workspace("--project-name", "missing-chain-template")
        self.run_state_command("start-day", root, "--day", "1")
        day_dir = root / "Day01"
        before_json = self.state_path(root).read_bytes()
        with mock.patch.object(module, "TEMPLATE_ROOT", missing_templates):
            with self.assertRaisesRegex(module.WorkspaceError, "template"):
                module.command_begin_chain(
                    SimpleNamespace(
                        learning_root=str(root), day=1, name="主线"
                    )
                )
        self.assertFalse((day_dir / "01-主线").exists())
        self.assertFalse(
            any(
                path.name.startswith(".01-主线.tmp-")
                for path in day_dir.iterdir()
            )
        )
        self.assertEqual(self.state_path(root).read_bytes(), before_json)

    def test_unknown_tokens_leave_no_half_workspace_or_chain(self) -> None:
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

        root = self.init_workspace("--project-name", "unknown-chain-token")
        self.run_state_command("start-day", root, "--day", "1")
        day_dir = root / "Day01"
        chain_template = poisoned / "chain-brief.md"
        chain_template.write_text(
            (real_templates / "chain-brief.md").read_text(encoding="utf-8")
            + "{{UNKNOWN}}\n",
            encoding="utf-8",
        )
        before_json = self.state_path(root).read_bytes()
        with mock.patch.object(module, "TEMPLATE_ROOT", poisoned):
            with self.assertRaisesRegex(module.WorkspaceError, "UNKNOWN"):
                module.command_begin_chain(
                    SimpleNamespace(
                        learning_root=str(root), day=1, name="主线"
                    )
                )
        self.assertFalse((day_dir / "01-主线").exists())
        self.assertFalse(
            any(
                path.name.startswith(".01-主线.tmp-")
                for path in day_dir.iterdir()
            )
        )
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

    def test_init_ignores_legacy_stage_ranges_key(self) -> None:
        data = roadmap()
        data["stage_ranges"] = [[0, 30], [31, 60], [61, 100]]
        self.write_roadmap(data)

        root = self.init_workspace()

        state = json.loads((root / "学习状态.json").read_text(encoding="utf-8"))
        self.assertNotIn("stage_ranges", state)
        self.assertNotIn("stages", state)
        self.assertNotIn("sources", state)
        self.assertEqual(state["chains"], [])
        self.assertEqual(state["schema_version"], 3)

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

        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(str(uuid.UUID(state["learning_root_id"])), state["learning_root_id"])
        self.assertEqual(marker["learning_root_id"], state["learning_root_id"])
        self.assertEqual(Path(state["learning_root"]), learning_root.resolve())
        self.assertEqual(Path(state["repository"]), self.repo.resolve())
        self.assertEqual(state["route_title"], "Demo 学习路线")
        self.assertEqual(state["route_days"], 2)
        self.assertIsNone(state["current"]["day"])
        self.assertIsNone(state["current"]["chain"])
        self.assertIsNone(state["current"]["last_source"])
        self.assertEqual(state["current"]["resume_marker"], "")
        self.assertEqual(state["current"]["closed_flows"], [])
        self.assertEqual(state["chains"], [])
        self.assertNotIn("sources", state)
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

    def test_begin_chain_creates_chain_dir_with_marker_brief_and_artifacts(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")

        result = self.begin_chain(root, 1, "入口分析")
        created = Path(result.stdout.strip())

        self.assertEqual(created, root / "Day01" / "01-入口分析")
        self.assertTrue(created.is_dir())
        self.assertTrue((created / "链说明.md").is_file())
        self.assertTrue((created / "产物").is_dir())
        self.assertIn(
            "源码入口", (created / "链说明.md").read_text(encoding="utf-8")
        )
        marker = json.loads((created / ".learning-chain.json").read_text(encoding="utf-8"))
        self.assertEqual(
            marker,
            {
                "kind": "source-code-learning-chain",
                "marker_version": 1,
                "schema_version": 3,
                "learning_root_id": self.read_state(root)["learning_root_id"],
                "day": 1,
                "order": 1,
                "name": "入口分析",
            },
        )
        state = self.read_state(root)
        self.assertEqual(len(state["chains"]), 1)
        self.assertEqual(state["chains"][0]["status"], "active")
        self.assertEqual(state["chains"][0]["path"], "Day01/01-入口分析")
        self.assertEqual(state["current"]["chain"]["path"], "Day01/01-入口分析")
        self.assertEqual(active_chain_count(root), 1)

    def test_second_begin_chain_seals_first_and_advances_order(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        first_dir = Path(self.begin_chain(root, 1, "入口").stdout.strip())
        first_hash = tree_hash(first_dir)

        second_dir = Path(self.begin_chain(root, 1, "依赖").stdout.strip())

        self.assertEqual(first_dir, root / "Day01" / "01-入口")
        self.assertEqual(second_dir, root / "Day01" / "02-依赖")
        state = self.read_state(root)
        self.assertEqual(len(state["chains"]), 2)
        self.assertEqual(state["chains"][0]["status"], "sealed")
        self.assertEqual(state["chains"][0]["path"], "Day01/01-入口")
        self.assertIn("sealed_at", state["chains"][0])
        self.assertEqual(state["chains"][1]["status"], "active")
        self.assertEqual(state["chains"][1]["path"], "Day01/02-依赖")
        self.assertEqual(state["current"]["chain"]["path"], "Day01/02-依赖")
        self.assertEqual(tree_hash(first_dir), first_hash)
        self.assertEqual(active_chain_count(root), 1)

    def test_begin_chain_rejects_when_no_active_day_or_wrong_day(self) -> None:
        root = self.init_workspace()
        before = self.state_path(root).read_bytes()

        self.begin_chain(root, 1, "无活跃Day", ok=False)
        self.assertEqual(self.state_path(root).read_bytes(), before)

        self.run_state_command("start-day", root, "--day", "1")
        before = self.state_path(root).read_bytes()
        self.begin_chain(root, 2, "错误Day", ok=False)
        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertFalse((root / "Day02" / "01-错误Day").exists())

    def test_begin_chain_rejects_unsafe_name_without_writes(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        day_dir = root / "Day01"
        cases = ("../escape", ".", "..", "bad/name", "bad\nname", "")
        for name in cases:
            with self.subTest(name=repr(name)):
                before = self.state_path(root).read_bytes()
                before_day = tree_hash(day_dir)
                self.begin_chain(root, 1, name, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before)
                self.assertEqual(tree_hash(day_dir), before_day)
                self.assertEqual(active_chain_count(root), 0)

    def test_complete_checkpoint_updates_progress_without_touching_chains(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        chain_before = self.read_state(root)["chains"]
        chain_dir = root / "Day01" / "01-主线"
        chain_dir_hash = tree_hash(chain_dir)

        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-flow")

        state = self.read_state(root)
        self.assertEqual(state["current"]["progress"], 70)
        self.assertEqual(state["chains"], chain_before)
        self.assertEqual(state["current"]["chain"]["path"], "Day01/01-主线")
        self.assertEqual(tree_hash(chain_dir), chain_dir_hash)
        self.assertFalse((root / "Day01" / "02-").exists())
        self.assertEqual(active_chain_count(root), 1)

    def test_reopen_checkpoint_records_reason_and_restores_progress(self) -> None:
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

        state = self.read_state(root)
        self.assertEqual(state["current"]["progress"], 20)
        self.assertEqual(state["history"][-1]["reason"], "需要重新理解调用链")
        self.assertEqual(state["days"][0]["checkpoints"][1]["status"], "pending")

    def test_reopen_then_recomplete_is_idempotent_on_chains(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "基线链")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command(
            "reopen-checkpoint", root, "--checkpoint", "d1-entry", "--reason", "one"
        )
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        self.run_state_command(
            "reopen-checkpoint", root, "--checkpoint", "d1-entry", "--reason", "two"
        )
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")

        state = self.read_state(root)
        self.assertEqual(len(state["chains"]), 1)
        self.assertEqual(state["chains"][0]["status"], "active")
        self.assertEqual(active_chain_count(root), 1)

    def test_complete_day_seals_active_chain_then_allows_next_day(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        for checkpoint in ("d1-entry", "d1-flow", "d1-summary"):
            self.run_state_command("complete-checkpoint", root, "--checkpoint", checkpoint)
        self.run_state_command("complete-day", root, "--day", "1")

        between = self.read_state(root)
        self.assertEqual(between["days"][0]["status"], "completed")
        self.assertEqual(between["chains"][0]["status"], "sealed")
        self.assertIsNone(between["current"]["day"])
        self.assertIsNone(between["current"]["chain"])
        self.assertEqual(active_chain_count(root), 0)

        self.run_state_command("start-day", root, "--day", "2")
        state = self.read_state(root)
        self.assertEqual(state["current"]["day"], 2)
        self.assertEqual(state["days"][0]["status"], "completed")
        self.assertEqual(state["days"][1]["status"], "active")
        self.assertEqual(active_chain_count(root), 0)
        self.assertIsNone(state["current"]["chain"])

    def test_complete_day_succeeds_with_zero_chains(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        for checkpoint in ("d1-entry", "d1-flow", "d1-summary"):
            self.run_state_command("complete-checkpoint", root, "--checkpoint", checkpoint)
        before = self.state_path(root).read_bytes()

        self.run_state_command("complete-day", root, "--day", "1")

        state = self.read_state(root)
        self.assertEqual(state["days"][0]["status"], "completed")
        self.assertEqual(state["chains"], [])
        self.assertNotEqual(self.state_path(root).read_bytes(), before)

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
            (
                "begin-chain on wrong day",
                "begin-chain",
                ("--day", "2", "--name", "wrong"),
            ),
        ]
        for label, command, arguments in invalid_active_cases:
            with self.subTest(label=label):
                before = self.state_path(root).read_bytes()
                self.run_state_command(command, root, *arguments, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_two_active_chains_are_rejected_without_mutation(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "基线")
        state = self.read_state(root)
        duplicate = copy.deepcopy(state["chains"][0])
        duplicate["path"] = "Day01/02-foreign"
        duplicate["order"] = 2
        duplicate["name"] = "foreign"
        state["chains"].append(duplicate)
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

    def test_precommit_failures_preserve_authority_and_no_new_chain(self) -> None:
        module = load_cli_module()

        root = self.init_workspace()
        before = self.state_path(root).read_bytes()
        with mock.patch.object(module, "replace_authority_json", side_effect=OSError("authority fail")):
            with self.assertRaises(OSError):
                module.command_start_day(SimpleNamespace(learning_root=str(root), day=1))
        # Authority JSON is unchanged; the day-level files are legitimate start-day
        # output written before the authority commit, so they may remain on disk.
        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(active_chain_count(root), 0)
        state = self.read_state(root)
        self.assertIsNone(state["current"]["day"])
        self.assertIsNone(state["current"]["chain"])
        self.assertEqual(state["days"][0]["status"], "pending")

        self.run_state_command("start-day", root, "--day", "1")
        self.run_state_command("complete-checkpoint", root, "--checkpoint", "d1-entry")
        before = self.state_path(root).read_bytes()
        with mock.patch.object(module, "replace_authority_json", side_effect=OSError("authority fail")):
            with self.assertRaises(OSError):
                module.command_complete_checkpoint(
                    SimpleNamespace(learning_root=str(root), checkpoint="d1-flow")
                )
        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(active_chain_count(root), 0)

        before = self.state_path(root).read_bytes()
        with mock.patch.object(module, "replace_authority_json", side_effect=OSError("authority fail")):
            with self.assertRaises(OSError):
                module.command_begin_chain(
                    SimpleNamespace(
                        learning_root=str(root), day=1, name="预提交失败"
                    )
                )
        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(active_chain_count(root), 0)
        self.assertFalse((root / "Day01" / "01-预提交失败").exists())

    def test_retry_cleans_only_owned_unregistered_chain_residue(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        state = self.read_state(root)
        target = root / "Day01" / "01-主线"
        target.mkdir()
        (target / ".learning-chain.json").write_text(
            json.dumps(chain_marker(state, 1, 1, "主线")), encoding="utf-8"
        )
        (target / "stale.txt").write_text("stale", encoding="utf-8")

        self.begin_chain(root, 1, "主线")

        self.assertTrue(target.is_dir())
        self.assertFalse((target / "stale.txt").exists())
        self.assertEqual(active_chain_count(root), 1)

    def test_retry_rejects_and_preserves_foreign_chain_directory(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        target = root / "Day01" / "01-主线"
        target.mkdir()
        sentinel = target / "sentinel.bin"
        sentinel.write_bytes(b"foreign")
        before = self.state_path(root).read_bytes()

        self.begin_chain(root, 1, "主线", ok=False)

        self.assertEqual(self.state_path(root).read_bytes(), before)
        self.assertEqual(sentinel.read_bytes(), b"foreign")

    def test_retry_preserves_user_data_when_generated_marker_fields_are_wrong(self) -> None:
        wrong_fields = {
            "kind": "user-directory",
            "marker_version": 2,
            "schema_version": 999,
            "learning_root_id": str(uuid.uuid4()),
            "day": 2,
            "order": 5,
            "name": "different",
        }
        for index, (field, wrong_value) in enumerate(wrong_fields.items(), start=1):
            with self.subTest(field=field):
                root = self.init_workspace("--project-name", f"forged-marker-{index}")
                self.run_state_command("start-day", root, "--day", "1")
                state = self.read_state(root)
                target = root / "Day01" / "01-主线"
                target.mkdir()
                marker = chain_marker(state, 1, 1, "主线")
                marker[field] = wrong_value
                (target / ".learning-chain.json").write_text(
                    json.dumps(marker), encoding="utf-8"
                )
                sentinel = target / "user-data.bin"
                sentinel.write_bytes(b"must-survive")
                before_json = self.state_path(root).read_bytes()
                before_target = tree_hash(target)

                self.begin_chain(root, 1, "主线", ok=False)

                self.assertEqual(self.state_path(root).read_bytes(), before_json)
                self.assertEqual(tree_hash(target), before_target)
                self.assertEqual(sentinel.read_bytes(), b"must-survive")

    def test_active_chain_marker_mismatch_is_rejected_by_every_state_command(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        marker_path = root / "Day01" / "01-主线" / ".learning-chain.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["learning_root_id"] = str(uuid.uuid4())
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
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
                self.assertIn("chain", result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(tree_hash(root), before_tree)

    def test_active_chain_directory_and_marker_are_validated(self) -> None:
        missing_root = self.init_workspace("--project-name", "missing-chain")
        self.run_state_command("start-day", missing_root, "--day", "1")
        self.begin_chain(missing_root, 1, "主线")
        missing_state = self.read_state(missing_root)
        missing_path = missing_root / missing_state["current"]["chain"]["path"]
        shutil.rmtree(missing_path)
        missing_before = tree_hash(missing_root)

        missing_result = self.run_state_command("sync-state", missing_root, ok=False)

        self.assertIn("active chain directory", missing_result.stderr)
        self.assertNotIn("Traceback", missing_result.stderr)
        self.assertEqual(tree_hash(missing_root), missing_before)

        marker_root = self.init_workspace("--project-name", "bad-chain-marker")
        self.run_state_command("start-day", marker_root, "--day", "1")
        self.begin_chain(marker_root, 1, "主线")
        marker_state = self.read_state(marker_root)
        marker_path = marker_root / marker_state["current"]["chain"]["path"]
        marker_data = json.loads(
            (marker_path / ".learning-chain.json").read_text(encoding="utf-8")
        )
        marker_data["learning_root_id"] = str(uuid.uuid4())
        (marker_path / ".learning-chain.json").write_text(
            json.dumps(marker_data), encoding="utf-8"
        )
        marker_before = tree_hash(marker_root)

        marker_result = self.run_state_command("sync-state", marker_root, ok=False)

        self.assertIn("chain marker", marker_result.stderr)
        self.assertNotIn("Traceback", marker_result.stderr)
        self.assertEqual(tree_hash(marker_root), marker_before)

    def test_active_chain_path_must_match_day_order_and_name(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        state = self.read_state(root)
        state["chains"][0]["path"] = "Day01/not-the-generated-name"
        state["current"]["chain"]["path"] = "Day01/not-the-generated-name"
        self.state_path(root).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        before_tree = tree_hash(root)

        result = self.run_state_command("sync-state", root, ok=False)

        self.assertIn("chain path", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(tree_hash(root), before_tree)

    def test_start_day_rejects_symlinked_day_parent_without_external_writes(self) -> None:
        root = self.init_workspace()
        day_directory = root / "Day01"
        (day_directory / "口述回答归档.md").unlink()
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
        self.assertEqual(committed["days"][0]["status"], "active")
        self.assertTrue((root / "Day01" / "问题与解答.md").is_file())
        self.assertTrue((root / "Day01" / "当日总结.md").is_file())
        self.assertTrue(
            (root / "Day01" / "当日总览产物" / "当日统一总结.md").is_file()
        )

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
            ("begin-chain", ("--learning-root", str(root), "--day", "1", "--name", "主线")),
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

    def test_copied_or_forged_learning_root_is_rejected_by_state_commands(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        copied_root = self.base / "copied-learning-root"
        shutil.copytree(root, copied_root)
        before_copy = tree_hash(copied_root)

        copied = self.run_state_command("sync-state", copied_root, ok=False)

        self.assertIn("learning-root", copied.stderr)
        self.assertEqual(tree_hash(copied_root), before_copy)

        state = self.read_state(root)
        state["learning_root"] = str(copied_root.resolve())
        self.state_path(root).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        before_forged = tree_hash(root)
        forged = self.run_state_command("sync-state", root, ok=False)
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
            result = self.run_state_command("sync-state", root_inside, ok=False)
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
            result = self.run_state_command("sync-state", nested_root, ok=False)
            self.assertIn("sibling", result.stderr)
            self.assertEqual(tree_hash(nested_root), before_nested)

    def test_symlinked_resume_source_is_rejected_without_writes(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        external_source = self.base / "external-source.py"
        external_source.write_text("outside\n", encoding="utf-8")
        source_link = self.repo / "src" / "escape.py"
        source_link.symlink_to(external_source)
        self.repo_hash = tree_hash(self.repo)
        before_json = self.state_path(root).read_bytes()
        try:
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
        finally:
            source_link.unlink(missing_ok=True)
            self.repo_hash = tree_hash(self.repo)

    def test_begin_chain_day_swap_race_never_writes_through_external_symlink(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        day_dir = root / "Day01"
        held_day = day_dir.parent / ".held-day"
        external = self.base / "external-race-target"
        external.mkdir()
        sentinel = external / "sentinel.bin"
        sentinel.write_bytes(b"user-owned")
        before_json = self.state_path(root).read_bytes()
        before_day = tree_hash(day_dir)
        before_external = tree_hash(external)
        native_prepare = module._prepare_chain_target

        def swap_day(root_arg, state, day, order, name):
            target = native_prepare(root_arg, state, day, order, name)
            day_dir.rename(held_day)
            day_dir.symlink_to(external, target_is_directory=True)
            return target

        try:
            with mock.patch.object(
                module, "_prepare_chain_target", side_effect=swap_day
            ):
                with self.assertRaises(module.WorkspaceError):
                    module.command_begin_chain(
                        SimpleNamespace(
                            learning_root=str(root), day=1, name="主线"
                        )
                    )
        finally:
            if day_dir.is_symlink():
                day_dir.unlink()
            if held_day.exists():
                held_day.rename(day_dir)

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(tree_hash(day_dir), before_day)
        self.assertEqual(tree_hash(external), before_external)
        self.assertEqual(sentinel.read_bytes(), b"user-owned")
        self.assertFalse(
            any(path.name.startswith(".01-主线.tmp-") for path in external.iterdir())
        )
        self.assertFalse((external / "01-主线").exists())

    def test_sealed_chain_corruption_is_rejected_by_every_state_command(self) -> None:
        source = self.repo / "src" / "main.py"
        corruption_kinds = ("missing", "symlink", "forged marker")
        for index, corruption in enumerate(corruption_kinds, start=1):
            with self.subTest(corruption=corruption):
                root = self.init_workspace("--project-name", f"sealed-corrupt-{index}")
                self.run_state_command("start-day", root, "--day", "1")
                self.begin_chain(root, 1, "主线")
                self.begin_chain(root, 1, "第二链")  # seals 主线
                sealed = root / "Day01" / "01-主线"
                external = self.base / f"sealed-external-{index}"
                external_hash = None
                if corruption == "missing":
                    shutil.rmtree(sealed)
                elif corruption == "symlink":
                    shutil.move(str(sealed), str(external))
                    sealed.symlink_to(external, target_is_directory=True)
                    external_hash = tree_hash(external)
                else:
                    marker_path = sealed / ".learning-chain.json"
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
                        ("--checkpoint", "d1-entry", "--reason", "corrupt sealed chain"),
                    ),
                    ("set-resume", ("--source", str(source), "--marker", "entry")),
                    ("complete-day", ("--day", "1")),
                )
                for command, arguments in commands:
                    with self.subTest(corruption=corruption, command=command):
                        result = self.run_state_command(
                            command, root, *arguments, ok=False
                        )
                        self.assertIn("chain", result.stderr.lower())
                        self.assertNotIn("Traceback", result.stderr)
                        self.assertEqual(tree_hash(root), before_tree)
                        if external_hash is not None:
                            self.assertEqual(tree_hash(external), external_hash)

    def test_implemented_command_help_exits_zero(self) -> None:
        root_help = self.run_cli("--help")
        self.assertEqual(root_help.returncode, 0)
        for command in (
            "init",
            "start-day",
            "begin-chain",
            "complete-checkpoint",
            "reopen-checkpoint",
            "set-resume",
            "complete-day",
            "sync-state",
        ):
            with self.subTest(command=command):
                result = self.run_cli(command, "--help")
                self.assertEqual(result.returncode, 0)

    def test_help_lists_state_commands_without_add_source(self) -> None:
        root_help = self.run_cli("--help")
        self.assertIn("begin-chain", root_help.stdout)
        self.assertIn("set-resume", root_help.stdout)
        self.assertIn("register-artifact", root_help.stdout)
        self.assertNotIn("add-source", root_help.stdout)

        self.run_cli("add-source", "--help", ok=False)

        begin_chain_help = self.run_cli("begin-chain", "--help")
        self.assertEqual(begin_chain_help.returncode, 0)
        self.assertIn("--day", begin_chain_help.stdout)
        self.assertIn("--name", begin_chain_help.stdout)

        set_resume_help = self.run_cli("set-resume", "--help")
        self.assertEqual(set_resume_help.returncode, 0)
        self.assertIn("--source", set_resume_help.stdout)
        self.assertIn("--marker", set_resume_help.stdout)

    def test_register_day_overview_artifact_records_and_projects_idempotently(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "entry-sequence.html"
        artifact.write_text("<html>day flow</html>\n", encoding="utf-8")

        first = self.register_artifact(root, "day-overview", overview, artifact)
        self.assertEqual(Path(first.stdout.strip()), artifact.resolve())
        first_state = self.read_state(root)
        self.assertEqual(len(first_state["artifacts"]), 1)
        record = first_state["artifacts"][0]
        self.assertEqual(record["canonical_artifact_path"], str(artifact.resolve()))
        self.assertEqual(record["owner_id"], "day-overview:1")
        self.assertEqual(record["owner_kind"], "day-overview")
        self.assertEqual(record["relative_owner_dir"], "Day01/当日总览产物")
        self.assertEqual(record["relative_artifact_path"], "entry-sequence.html")
        summary = overview / "当日统一总结.md"
        projected = summary.read_text(encoding="utf-8")
        self.assertIn("`entry-sequence.html`", projected)
        self.assertEqual(projected.count("entry-sequence.html"), 1)

        self.register_artifact(root, "day-overview", overview, artifact)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)
        self.assertEqual(
            summary.read_text(encoding="utf-8").count("entry-sequence.html"),
            1,
        )

    def test_register_day_overview_artifact_records_in_overview_markdown(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "day-framework.svg"
        artifact.write_text("<svg/>\n", encoding="utf-8")

        self.register_artifact(root, "day-overview", overview, artifact)

        state = self.read_state(root)
        self.assertEqual(len(state["artifacts"]), 1)
        self.assertEqual(
            state["artifacts"][0]["owner_id"], "day-overview:1"
        )
        summary = (overview / "当日统一总结.md").read_text(encoding="utf-8")
        self.assertIn("`day-framework.svg`", summary)

    def test_register_chain_artifact_records_without_markdown_projection(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        chain_dir = root / "Day01" / "01-主线"
        chain_artifacts = chain_dir / "产物"
        artifact = chain_artifacts / "call-tree.dot"
        artifact.write_text("digraph {}\n", encoding="utf-8")

        self.register_artifact(root, "chain", chain_artifacts, artifact)

        state = self.read_state(root)
        self.assertEqual(len(state["artifacts"]), 1)
        record = state["artifacts"][0]
        self.assertEqual(record["owner_kind"], "chain")
        self.assertEqual(record["owner_id"], "chain:Day01/01-主线")
        self.assertEqual(record["relative_owner_dir"], "Day01/01-主线/产物")
        self.assertEqual(record["relative_artifact_path"], "call-tree.dot")
        brief = (chain_dir / "链说明.md").read_text(encoding="utf-8")
        self.assertNotIn("call-tree.dot", brief)

    def test_register_artifact_rejects_unsafe_owner_and_artifact_inputs(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "当日总览产物"
        valid = overview / "valid.html"
        valid.write_text("ok\n", encoding="utf-8")
        outside = self.base / "outside.html"
        outside.write_text("outside\n", encoding="utf-8")
        directory = overview / "not-a-file"
        directory.mkdir()
        artifact_link = overview / "escape.html"
        artifact_link.symlink_to(outside)
        owner_link = self.base / "owner-link"
        owner_link.symlink_to(overview, target_is_directory=True)
        pending_overview = root / "Day02" / "当日总览产物"
        pending_overview.mkdir()
        pending_artifact = pending_overview / "pending.html"
        pending_artifact.write_text("pending\n", encoding="utf-8")
        forged_chain_artifacts = root / "Day01" / "99-伪造链" / "产物"
        forged_chain_artifacts.mkdir(parents=True)
        forged_artifact = forged_chain_artifacts / "x.html"
        forged_artifact.write_text("x\n", encoding="utf-8")
        before = self.state_path(root).read_bytes()

        cases = (
            ("day-overview", owner_link, valid),
            ("day-overview", overview / ".." / "当日总览产物", valid),
            ("day-overview", overview, outside),
            ("day-overview", overview, artifact_link),
            ("day-overview", overview, directory),
            ("day-overview", pending_overview, pending_artifact),
            ("chain", overview, valid),
            ("chain", forged_chain_artifacts, forged_artifact),
            ("source", overview, valid),
        )
        for owner_kind, owner, artifact in cases:
            with self.subTest(owner_kind=owner_kind, owner=owner, artifact=artifact):
                self.register_artifact(root, owner_kind, owner, artifact, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_register_artifact_rejects_completed_day_overview_owner(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "当日总览产物"
        day_artifact = overview / "sealed.svg"
        day_artifact.write_text("sealed\n", encoding="utf-8")
        for cp in ("d1-entry", "d1-flow", "d1-summary"):
            self.run_state_command("complete-checkpoint", root, "--checkpoint", cp)
        self.run_state_command("complete-day", root, "--day", "1")

        before = self.state_path(root).read_bytes()
        self.register_artifact(root, "day-overview", overview, day_artifact, ok=False)
        self.assertEqual(self.state_path(root).read_bytes(), before)

    def test_register_artifact_rejects_sealed_chain_owner(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        chain_artifacts = root / "Day01" / "01-主线" / "产物"
        artifact = chain_artifacts / "old.html"
        artifact.write_text("old\n", encoding="utf-8")
        # Seal it by starting a second chain
        self.begin_chain(root, 1, "第二链")
        before = self.state_path(root).read_bytes()

        self.register_artifact(root, "chain", chain_artifacts, artifact, ok=False)

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
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "diagram.html"
        artifact.write_text("diagram\n", encoding="utf-8")
        summary = overview / "当日统一总结.md"
        summary.write_text("human text without markers\n", encoding="utf-8")
        before_json = self.state_path(root).read_bytes()
        before_summary = summary.read_bytes()

        self.register_artifact(root, "day-overview", overview, artifact, ok=False)

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(summary.read_bytes(), before_summary)

    def test_register_artifact_transaction_and_retry_repair_projection(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "sequence.html"
        artifact.write_text("sequence\n", encoding="utf-8")
        summary = overview / "当日统一总结.md"
        original = summary.read_text(encoding="utf-8")
        customized = original.replace(
            "- 整体框架联系：待整理", "- 整体框架联系：人工内容，必须逐字保留"
        )
        self.assertNotEqual(customized, original)
        with summary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(customized)
        before_json = self.state_path(root).read_bytes()
        before_summary = summary.read_bytes()
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="day-overview",
            owner_dir=str(overview),
            artifact=str(artifact),
        )

        with mock.patch.object(
            module, "artifact_pre_authority_seam", side_effect=OSError("pre commit")
        ):
            with self.assertRaises(OSError):
                module.command_register_artifact(namespace)
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(summary.read_bytes(), before_summary)
        self.assertEqual(self.read_state(root)["artifacts"], [])

        with mock.patch.object(
            module, "replace_authority_json", side_effect=OSError("JSON commit failed")
        ):
            with self.assertRaises(OSError):
                module.command_register_artifact(namespace)
        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertEqual(summary.read_bytes(), before_summary)
        self.assertEqual(self.read_state(root)["artifacts"], [])

        with mock.patch.object(
            module, "replace_owner_markdown", side_effect=OSError("projection failed")
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "authoritative JSON.*retry"):
                module.command_register_artifact(namespace)

        committed = self.read_state(root)
        self.assertEqual(len(committed["artifacts"]), 1)
        self.assertEqual(summary.read_bytes(), before_summary)

        module.command_register_artifact(namespace)
        repaired = summary.read_text(encoding="utf-8")
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
        self.begin_chain(root, 1, "主线")
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "flow.html"
        artifact.write_text("flow\n", encoding="utf-8")
        self.register_artifact(root, "day-overview", overview, artifact)
        state = self.read_state(root)
        forged = copy.deepcopy(state["artifacts"][0])
        forged["owner_id"] = "chain:Day01/01-主线"
        forged["owner_kind"] = "chain"
        forged["relative_owner_dir"] = "Day01/01-主线/产物"
        state["artifacts"].append(forged)
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
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "race.html"
        artifact.write_text("race\n", encoding="utf-8")
        summary = overview / "当日统一总结.md"
        original_summary = summary.read_bytes()
        outside = self.base / "outside-summary.md"
        outside.write_bytes(b"outside sentinel")
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="day-overview",
            owner_dir=str(overview),
            artifact=str(artifact),
        )

        def replace_summary_with_symlink(*_args) -> None:
            summary.unlink()
            summary.symlink_to(outside)

        with mock.patch.object(
            module,
            "artifact_pre_authority_seam",
            side_effect=replace_summary_with_symlink,
        ):
            with self.assertRaises(module.WorkspaceError):
                module.command_register_artifact(namespace)

        self.assertEqual(outside.read_bytes(), b"outside sentinel")
        self.assertEqual(self.read_state(root)["artifacts"], [])
        summary.unlink()
        summary.write_bytes(original_summary)

    def test_register_artifact_owner_and_file_swap_races_fail_closed(self) -> None:
        module = load_cli_module()
        for race_kind in ("artifact", "owner"):
            with self.subTest(race_kind=race_kind):
                root = self.init_workspace(
                    "--project-name", f"artifact-race-{race_kind}"
                )
                self.run_state_command("start-day", root, "--day", "1")
                overview = root / "Day01" / "当日总览产物"
                artifact = overview / "race.html"
                artifact.write_text("original artifact\n", encoding="utf-8")
                moved_owner = overview.with_name(overview.name + "-moved")
                external_owner = self.base / f"external-owner-{race_kind}"
                external_owner.mkdir()
                (external_owner / "当日统一总结.md").write_bytes(
                    b"outside summary sentinel"
                )
                namespace = SimpleNamespace(
                    learning_root=str(root),
                    owner_kind="day-overview",
                    owner_dir=str(overview),
                    artifact=str(artifact),
                )

                def swap_after_prepare(*_args) -> None:
                    if race_kind == "artifact":
                        artifact.unlink()
                        artifact.write_text("replacement artifact\n", encoding="utf-8")
                    else:
                        overview.rename(moved_owner)
                        overview.symlink_to(external_owner, target_is_directory=True)

                try:
                    with mock.patch.object(
                        module,
                        "artifact_pre_authority_seam",
                        side_effect=swap_after_prepare,
                    ):
                        with self.assertRaises(module.WorkspaceError):
                            module.command_register_artifact(namespace)
                finally:
                    if race_kind == "owner" and overview.is_symlink():
                        overview.unlink()
                        moved_owner.rename(overview)

                raw_state = json.loads(self.state_path(root).read_text(encoding="utf-8"))
                self.assertEqual(raw_state["artifacts"], [])
                self.assertEqual(
                    (external_owner / "当日统一总结.md").read_bytes(),
                    b"outside summary sentinel",
                )
                if race_kind == "artifact":
                    self.assertEqual(
                        artifact.read_text(encoding="utf-8"), "replacement artifact\n"
                    )

    def test_registered_artifacts_remain_valid_after_owner_chain_is_sealed(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        chain_dir = root / "Day01" / "01-主线"
        chain_artifacts = chain_dir / "产物"
        artifact = chain_artifacts / "durable.html"
        artifact.write_text("durable\n", encoding="utf-8")
        self.register_artifact(root, "chain", chain_artifacts, artifact)

        self.begin_chain(root, 1, "第二链")  # seals 主线
        self.run_state_command("sync-state", root)

        state = self.read_state(root)
        self.assertEqual(len(state["artifacts"]), 1)
        self.assertEqual(state["chains"][0]["status"], "sealed")
        self.assertEqual(state["chains"][0]["path"], "Day01/01-主线")

    def test_register_artifact_fails_closed_without_dir_fd_primitives(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "unsupported.html"
        artifact.write_text("unsupported\n", encoding="utf-8")
        before_json = self.state_path(root).read_bytes()

        with mock.patch.object(module.os, "supports_dir_fd", set()):
            with self.assertRaisesRegex(module.WorkspaceError, "directory-fd"):
                module.command_register_artifact(
                    SimpleNamespace(
                        learning_root=str(root),
                        owner_kind="day-overview",
                        owner_dir=str(overview),
                        artifact=str(artifact),
                    )
                )

        self.assertEqual(self.state_path(root).read_bytes(), before_json)
        self.assertNotIn(
            "unsupported.html",
            (overview / "当日统一总结.md").read_text(encoding="utf-8"),
        )

    def test_register_artifact_never_overwrites_concurrent_in_place_human_edits(self) -> None:
        module = load_cli_module()
        for phase in ("pre-authority", "post-authority"):
            with self.subTest(phase=phase):
                root = self.init_workspace(
                    "--project-name", f"in-place-edit-{phase}"
                )
                self.run_state_command("start-day", root, "--day", "1")
                overview = root / "Day01" / "当日总览产物"
                artifact = overview / "concurrent.html"
                artifact.write_text("artifact\n", encoding="utf-8")
                summary = overview / "当日统一总结.md"
                before_json = self.state_path(root).read_bytes()
                human_append = f"\nHUMAN APPEND {phase}\n".encode("utf-8")
                namespace = SimpleNamespace(
                    learning_root=str(root),
                    owner_kind="day-overview",
                    owner_dir=str(overview),
                    artifact=str(artifact),
                )

                def append_in_place(*_args) -> None:
                    with summary.open("ab") as handle:
                        handle.write(human_append)

                seam = (
                    "artifact_pre_authority_seam"
                    if phase == "pre-authority"
                    else "artifact_post_authority_seam"
                )
                with mock.patch.object(module, seam, side_effect=append_in_place):
                    with self.assertRaises(module.WorkspaceError):
                        module.command_register_artifact(namespace)

                self.assertTrue(summary.read_bytes().endswith(human_append))
                if phase == "pre-authority":
                    self.assertEqual(self.state_path(root).read_bytes(), before_json)
                    self.assertEqual(self.read_state(root)["artifacts"], [])
                else:
                    self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

                module.command_register_artifact(namespace)
                repaired = summary.read_bytes()
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
        overview = root / "Day01" / "当日总览产物"
        summary = overview / "当日统一总结.md"
        before_json = self.state_path(root).read_bytes()
        before_summary = summary.read_bytes()
        bad_names = (
            "bad\nname.html",
            "bad<!-- learning-coach:artifacts:end -->.html",
            "bad<!-- learning-coach:artifacts:start -->.html",
        )
        for name in bad_names:
            with self.subTest(name=repr(name)):
                artifact = overview / name
                artifact.write_text("bad name\n", encoding="utf-8")
                self.register_artifact(root, "day-overview", overview, artifact, ok=False)
                self.assertEqual(self.state_path(root).read_bytes(), before_json)
                self.assertEqual(summary.read_bytes(), before_summary)

    def test_projection_retry_is_allowed_after_owner_chain_becomes_sealed(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        chain_dir = root / "Day01" / "01-主线"
        chain_artifacts = chain_dir / "产物"
        artifact = chain_artifacts / "chain-repair.html"
        artifact.write_text("artifact\n", encoding="utf-8")
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="chain",
            owner_dir=str(chain_artifacts),
            artifact=str(artifact),
        )

        # Chain owner has no markdown projection — registration succeeds atomically.
        module.command_register_artifact(namespace)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

        self.begin_chain(root, 1, "第二链")  # seals 主线
        state = self.read_state(root)
        self.assertEqual(state["chains"][0]["status"], "sealed")

        # Retry is still allowed — the chain artifact has no markdown to project
        module.command_register_artifact(namespace)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

    def test_artifact_physical_identity_is_unique_and_replacement_is_rejected(self) -> None:
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        self.begin_chain(root, 1, "主线")
        overview = root / "Day01" / "当日总览产物"
        original = overview / "CaseFlow.HTML"
        alias = overview / "caseflow.html"
        original.write_text("same physical file\n", encoding="utf-8")
        if not alias.exists():
            os.link(original, alias)
        self.assertTrue(os.path.samefile(original, alias))

        self.register_artifact(root, "day-overview", overview, original)
        self.register_artifact(root, "day-overview", overview, alias)

        state = self.read_state(root)
        self.assertEqual(len(state["artifacts"]), 1)
        record = state["artifacts"][0]
        metadata = original.stat()
        self.assertEqual(record["artifact_device"], metadata.st_dev)
        self.assertEqual(record["artifact_inode"], metadata.st_ino)
        chain_artifacts = root / "Day01" / "01-主线" / "产物"
        second_owner_alias = chain_artifacts / "same-physical.html"
        os.link(original, second_owner_alias)
        self.register_artifact(
            root, "chain", chain_artifacts, second_owner_alias, ok=False
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
            "./flow.html",
            "nested/../flow.html",
            "flow\\name.html",
        )
        for index, corrupted in enumerate(corruptions, start=1):
            with self.subTest(corrupted=corrupted):
                root = self.init_workspace(
                    "--project-name", f"artifact-relative-{index}"
                )
                self.run_state_command("start-day", root, "--day", "1")
                overview = root / "Day01" / "当日总览产物"
                artifact = overview / "flow.html"
                artifact.write_text("flow\n", encoding="utf-8")
                self.register_artifact(root, "day-overview", overview, artifact)
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
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "exchange-race.html"
        artifact.write_text("artifact\n", encoding="utf-8")
        summary = overview / "当日统一总结.md"
        human_append = b"\nHUMAN EDIT AT EXCHANGE\n"
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="day-overview",
            owner_dir=str(overview),
            artifact=str(artifact),
        )

        def append_at_exchange(*_args) -> None:
            with summary.open("ab") as handle:
                handle.write(human_append)

        with mock.patch.object(
            module,
            "owner_projection_exchange_seam",
            side_effect=append_at_exchange,
        ):
            with self.assertRaisesRegex(module.WorkspaceError, "authoritative JSON.*retry"):
                module.command_register_artifact(namespace)

        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)
        failed_projection = summary.read_bytes()
        self.assertTrue(failed_projection.endswith(human_append))
        self.assertNotIn(b"exchange-race.html", failed_projection)

        module.command_register_artifact(namespace)

        repaired = summary.read_bytes()
        self.assertIn(human_append, repaired)
        self.assertEqual(repaired.count(b"exchange-race.html"), 1)
        self.assertEqual(len(self.read_state(root)["artifacts"]), 1)

    def test_projection_exchange_never_publishes_replaced_temp_entry(self) -> None:
        module = load_cli_module()
        root = self.init_workspace()
        self.run_state_command("start-day", root, "--day", "1")
        overview = root / "Day01" / "当日总览产物"
        artifact = overview / "safe-candidate.html"
        artifact.write_text("artifact\n", encoding="utf-8")
        summary = overview / "当日统一总结.md"
        original_summary = summary.read_bytes()
        attacker_bytes = b"ATTACKER TEMP CONTENT\n"
        observed_temp_names: list[str] = []
        namespace = SimpleNamespace(
            learning_root=str(root),
            owner_kind="day-overview",
            owner_dir=str(overview),
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
        self.assertEqual(summary.read_bytes(), original_summary)
        self.assertNotIn(attacker_bytes, summary.read_bytes())
        self.assertEqual(len(observed_temp_names), 1)
        foreign_temp = overview / observed_temp_names[0]
        self.assertEqual(foreign_temp.read_bytes(), attacker_bytes)

        module.command_register_artifact(namespace)

        self.assertEqual(
            (overview / "当日统一总结.md").read_bytes().count(b"safe-candidate.html"),
            1,
        )
        self.assertEqual(foreign_temp.read_bytes(), attacker_bytes)

    def test_register_artifact_help_and_root_help_include_final_command(self) -> None:
        self.assertEqual(self.run_cli("register-artifact", "--help").returncode, 0)
        root_help = self.run_cli("--help")
        self.assertIn("register-artifact", root_help.stdout)
        self.assertNotIn("add-source", root_help.stdout)
        for command in (
            "init",
            "start-day",
            "begin-chain",
            "complete-checkpoint",
            "reopen-checkpoint",
            "set-resume",
            "register-artifact",
            "complete-day",
            "sync-state",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command, "--help").returncode, 0)
