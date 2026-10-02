"""Static contract tests for the AWS Terraform module and its root wrapper.

Terraform is neither needed nor run here. The files are read as text, the bootstrap
script is rendered the way ``templatefile()`` renders it and checked with ``bash -n``
(and ShellCheck when it is installed), and the optional API-token helper is exercised
against the application's real config loader.

They guard the places where the infrastructure and the application must agree, and
the properties that protect trading state, which are easy to "simplify" away by accident.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from tests._util import ROOT, isolated_config, temp_dir

MODULE = ROOT / "modules" / "crypto-hunter-ec2"
MODULE_TF = sorted(MODULE.glob("*.tf"))
ROOT_TF = sorted(ROOT.glob("*.tf"))
TEMPLATE = MODULE / "user_data.sh.tftpl"
HELPER = MODULE / "files" / "sync_api_token.py"
TOKEN_PARAMETER = "/crypto-hunter/api-token"


# --------------------------------------------------------------------------- HCL as text
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_only(hcl: str) -> str:
    """The file without full-line comments."""
    return "\n".join(line for line in hcl.splitlines() if not re.match(r"\s*(#|//)", line))


def _skip_string(text: str, i: int) -> int:
    """``i`` is just after an opening quote; returns the index after the closing one."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text.startswith(("$${", "%%{"), i):
            i += 3
        elif text.startswith(("${", "%{"), i):
            i = _skip_braces(text, i + 2)
        elif text[i] == '"':
            return i + 1
        else:
            i += 1
    return i


def _skip_braces(text: str, i: int) -> int:
    """``i`` is just after an opening brace; returns the index after its match."""
    depth = 1
    while i < len(text) and depth:
        if text[i] == '"':
            i = _skip_string(text, i + 1)
            continue
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return i


def blocks(text: str, kind: str) -> list[tuple[list[str], str]]:
    """``(labels, body)`` of every top-level ``kind "a" "b" { ... }`` block."""
    found = []
    for m in re.finditer(rf'^{kind}((?:[ \t]+"[^"]*")*)[ \t]*\{{', text, re.M):
        end = _skip_braces(text, m.end())
        found.append((re.findall(r'"([^"]*)"', m.group(1)), text[m.end():end - 1]))
    return found


def module_text() -> str:
    return "\n".join(code_only(read(p)) for p in MODULE_TF)


def declared(kind: str, text: str | None = None) -> dict[str, str]:
    text = module_text() if text is None else text
    return {labels[0]: body for labels, body in blocks(text, kind)}


# --------------------------------------------------------------------------- templatefile() stand-in
def template_variables(text: str) -> set[str]:
    """Names referenced as ${name}; ``$${`` is Terraform's escape for a literal ``${``."""
    return set(re.findall(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text))


def templatefile_keys() -> set[str]:
    main = code_only(read(MODULE / "main.tf"))
    start = main.index("templatefile(") + len("templatefile(")
    after_path = _skip_string(main, main.index('"', start) + 1)  # first argument: the template path
    opening = main.index("{", after_path)                         # second argument: the variable map
    body = main[opening + 1:_skip_braces(main, opening + 1) - 1]
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", body, re.M))


def render_user_data(*, token: bool, repo_url: str = "https://github.com/badalworld/AWESOME.git") -> str:
    values = {
        "repo_url": repo_url,
        "repo_ref": "main",
        "port": 8080,
        "data_volume_id": "vol-0123456789abcdef0",
        "apt_packages": "curl git python3-pip python3-venv" + (" python3-boto3" if token else ""),
        "token_parameter": TOKEN_PARAMETER if token else "",
        "aws_region": "ap-southeast-1",
        "sync_token_py": read(HELPER).rstrip("\n"),  # main.tf wraps the file in chomp()
    }

    def substitute(match: re.Match) -> str:
        if match.group(0) == "$${":
            return "${"
        return str(values[match.group(1)])

    return re.sub(r"\$\$\{|\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, read(TEMPLATE))


def load_helper() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sync_api_token", HELPER)
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ inside the module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# --------------------------------------------------------------------------- tests
class ProviderV6Contract(unittest.TestCase):
    def test_module_is_reusable_and_allows_only_aws_provider_v6(self) -> None:
        for path in MODULE_TF:
            self.assertIsNone(re.search(r'^\s*provider\s+"', code_only(read(path)), re.M),
                              f"{path.name}: a reusable module must not configure a provider; the caller does")
        for path in (MODULE / "versions.tf", ROOT / "versions.tf"):
            m = re.search(r'source\s*=\s*"hashicorp/aws"\s+version\s*=\s*"([^"]+)"', read(path))
            self.assertIsNotNone(m, f"{path}: hashicorp/aws is not pinned")
            self.assertRegex(m.group(1), r"^(~>\s*6\.\d+|>=\s*6\.\d+(\.\d+)?,\s*<\s*7(\.0)*)$",
                             f"{path}: the constraint must allow 6.x and nothing else")

    def test_no_arguments_that_provider_v6_removed_or_deprecated(self) -> None:
        text = "\n".join(code_only(read(p)) for p in MODULE_TF + ROOT_TF)
        forbidden = {
            r"^\s*vpc\s*=\s*(true|false)\s*$": "aws_eip.vpc was removed in v6: use domain = \"vpc\"",
            r"data\.aws_region\.\w+\.name\b": "data.aws_region.name is deprecated in v6: use .region",
            r"\bcpu_core_count\b|\bcpu_threads_per_core\b": "removed in v6: use cpu_options { core_count, threads_per_core }",
            r"\belastic_gpu_specifications\b|\belastic_inference_accelerator\b": "removed in v6",
            r"\buser_data_base64\b": "the bootstrap is plain text on purpose (v6 stores user_data in clear text, so it must hold no secrets)",
        }
        for pattern, why in forbidden.items():
            self.assertIsNone(re.search(pattern, text, re.M), why)

    def test_ami_comes_from_ssm_without_marking_it_sensitive(self) -> None:
        main = code_only(read(MODULE / "main.tf"))
        self.assertIn(".insecure_value", main, "an AMI id is not a secret; `.value` would hide it from every plan")
        self.assertNotRegex(main, r"aws_ssm_parameter\.\w+(\[\d+\])?\.value\b")


class RootWrapper(unittest.TestCase):
    def test_root_calls_the_module_with_valid_arguments(self) -> None:
        root_main = code_only(read(ROOT / "main.tf"))
        calls = {labels[0]: body for labels, body in blocks(root_main, "module")}
        self.assertIn("engine", calls)
        body = calls["engine"]
        self.assertIn('source = "./modules/crypto-hunter-ec2"', re.sub(r"\s+", " ", body))
        passed = set(re.findall(r"^\s*([a-z_]+)\s*=", body, re.M)) - {"source"}
        variables = declared("variable")
        self.assertFalse(passed - set(variables), f"module has no such variables: {passed - set(variables)}")
        required = {name for name, vbody in variables.items() if not re.search(r"^\s*default\s*=", vbody, re.M)}
        self.assertFalse(required - passed, f"required module variables not passed: {required - passed}")

    def test_root_still_offers_the_variables_and_outputs_existing_users_rely_on(self) -> None:
        root_vars = set(declared("variable", code_only(read(ROOT / "variables.tf"))))
        for name in ("region", "name", "instance_type", "volume_size_gb", "repo_url", "repo_ref",
                     "app_port", "key_name", "ssh_cidrs", "app_cidrs"):
            self.assertIn(name, root_vars, f"root variable {name} existed before the module and must keep working")
        root_outputs = set(declared("output", code_only(read(ROOT / "outputs.tf"))))
        for name in ("public_ip", "engine_url", "instance_id"):
            self.assertIn(name, root_outputs, f"root output {name} is used by the Netlify ENGINE_URL flow")

    def test_moved_blocks_point_at_real_module_resources_and_away_from_real_root_ones(self) -> None:
        root_main = code_only(read(ROOT / "main.tf"))
        module = module_text()
        resources = {(labels[0], labels[1]): body for labels, body in blocks(module, "resource")}
        root_resources = {(labels[0], labels[1]) for labels, _ in blocks(root_main, "resource")}
        moved = blocks(root_main, "moved")
        self.assertTrue(moved, "existing deployments keep their Elastic IP only if these moves exist")
        for _, body in moved:
            src = re.search(r"from\s*=\s*(\w+)\.(\w+)\s*$", body, re.M)
            dst = re.search(r"to\s*=\s*module\.\w+\.(\w+)\.(\w+)(\[0\])?\s*$", body, re.M)
            self.assertTrue(src and dst, f"unrecognised moved block: {body.strip()}")
            self.assertNotIn(src.groups()[:2], root_resources, "a moved `from` must no longer exist in the root")
            target = (dst.group(1), dst.group(2))
            self.assertIn(target, resources, f"moved target {target} does not exist in the module")
            counted = bool(re.search(r"^\s*count\s*=", resources[target], re.M))
            self.assertEqual(bool(dst.group(3)), counted, f"{target}: index in `to` must match count")


class DocumentationDrift(unittest.TestCase):
    def test_module_readme_documents_every_input_and_output(self) -> None:
        readme = read(MODULE / "README.md")
        for kind in ("variable", "output"):
            for name in declared(kind):
                self.assertIn(f"`{name}`", readme, f"module README does not mention {kind} `{name}`")


class BootstrapScript(unittest.TestCase):
    def test_templatefile_call_supplies_exactly_the_variables_the_template_uses(self) -> None:
        used, supplied = template_variables(read(TEMPLATE)), templatefile_keys()
        self.assertFalse(used - supplied, f"template uses variables main.tf does not pass: {used - supplied}")
        self.assertFalse(supplied - used, f"main.tf passes variables the template ignores: {supplied - used}")
        self.assertNotIn("%{", read(TEMPLATE).replace("%%{", ""), "keep the template to plain ${var} substitution")

    def test_user_data_is_ascii_and_far_below_the_ec2_limit(self) -> None:
        for path in (TEMPLATE, HELPER):
            self.assertTrue(read(path).isascii(), f"{path.name}: user_data limits are in bytes, keep it ASCII")
        worst = render_user_data(token=True, repo_url="https://example.com/" + "a" * 220 + ".git")
        self.assertLess(len(worst.encode()), 12000, "EC2 user_data is limited to 16 KB; keep generous headroom")

    @unittest.skipUnless(shutil.which("bash"), "bash is required")
    def test_rendered_script_is_valid_bash_and_shellcheck_clean(self) -> None:
        for token in (False, True):
            script = temp_dir() / f"user_data_{token}.sh"
            script.write_text(render_user_data(token=token), encoding="utf-8")
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if shutil.which("shellcheck"):  # optional: pip install shellcheck-py
                lint = subprocess.run(["shellcheck", "--shell=bash", "--severity=warning", str(script)],
                                      capture_output=True, text=True)
                self.assertEqual(lint.returncode, 0, lint.stdout)

    def test_embedded_helper_is_the_shipped_file(self) -> None:
        rendered = render_user_data(token=True)
        self.assertIn(read(HELPER).rstrip("\n") + "\nPY\n", rendered)

    def test_bootstrap_agrees_with_the_application_layout(self) -> None:
        script = read(TEMPLATE)
        config = read(ROOT / "config.toml")
        self.assertRegex(config, r'(?m)^data_dir\s*=\s*"data"', "the script mounts the volume at <app>/data")
        self.assertIn('DATA_DIR="$APP_DIR/data"', script)
        self.assertIn("ExecStart=$APP_DIR/.venv/bin/python run.py --port $PORT", script)
        self.assertIn("--port", read(ROOT / "run.py"))
        self.assertTrue((ROOT / "requirements.txt").is_file())
        self.assertIn("-r $APP_DIR/requirements.txt", script.replace('"', ""))
        self.assertIn("ReadWritePaths=$DATA_DIR", script, "the service may write only to the state directory")

    def test_state_protections_are_in_place(self) -> None:
        script = read(TEMPLATE)
        module = module_text()
        checks = {
            "RequiresMountsFor=$DATA_DIR": (script, "the engine must never start on the root disk if the volume is missing"),
            'SIGNATURES="$(wipefs --no-act': (script, "format only a device with no signature, and let errexit catch a failed probe"),
            "nofail": (script, "a missing data volume must not hang boot"),
            "final_snapshot = true": (module, "leave a snapshot behind if Terraform ever deletes the state volume"),
            "stop_instance_before_detaching = true": (module, "detach only after a clean shutdown of the engine"),
            "ignore_changes = [availability_zone]": (module, "never replace the state volume because a subnet pick drifted"),
            'http_tokens                 = "required"': (module, "IMDSv2 only"),
            "user_data_replace_on_change = true": (module, "the bootstrap runs once; changes need a fresh instance"),
        }
        for needle, (haystack, why) in checks.items():
            self.assertIn(needle, haystack, why)
        self.assertGreaterEqual(len(re.findall(r"^\s*encrypted\s*=\s*true\s*$", module, re.M)), 2,
                                "root and data volumes must both be encrypted")
        security_group = code_only(read(MODULE / "security_group.tf"))
        self.assertNotRegex(security_group, r"(?m)^\s*(ingress|egress)\s*\{",
                            "inline rules would fight callers who attach their own rules to security_group_id")


class ApiTokenHelper(unittest.TestCase):
    """The helper that copies the dashboard token from SSM into data/settings.json."""

    def _config_with_overrides(self, overrides: dict | None = None):
        cfg = isolated_config()
        settings = cfg.data_dir / "settings.json"
        if overrides is not None:
            settings.write_text(json.dumps(overrides))
        return cfg, settings

    def test_real_config_loader_accepts_what_the_helper_writes(self) -> None:
        helper = load_helper()
        cfg, settings = self._config_with_overrides({"web": {"allow_insecure_live": False}})
        self.assertEqual(cfg.get("web.api_token"), "")
        helper.merge_token(str(settings), "  s3cret-token \n")

        reloaded = type(cfg)(cfg.path)
        self.assertEqual(reloaded.get("web.api_token"), "s3cret-token")
        self.assertIs(reloaded.get("web.allow_insecure_live"), False, "other overrides must survive")
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in settings.parent.glob(".settings-*")], [], "no temp file left behind")

    def test_creates_the_file_when_the_dashboard_never_saved_a_setting(self) -> None:
        helper = load_helper()
        cfg, settings = self._config_with_overrides(None)
        self.assertFalse(settings.exists())
        helper.merge_token(str(settings), "tok")
        self.assertEqual(type(cfg)(cfg.path).get("web.api_token"), "tok")

    def test_never_clobbers_a_file_it_cannot_understand(self) -> None:
        helper = load_helper()
        for content in ("{not json", "[1, 2]", '{"web": 5}'):
            _, settings = self._config_with_overrides(None)
            settings.write_text(content)
            with self.assertRaises(ValueError):
                helper.merge_token(str(settings), "tok")
            self.assertEqual(settings.read_text(), content)
        _, settings = self._config_with_overrides({"web": {"allow_insecure_live": True}})
        before = settings.read_text()
        with self.assertRaises(ValueError):
            helper.merge_token(str(settings), "  \n")
        self.assertEqual(settings.read_text(), before, "an empty parameter must not wipe the stored token")

    def _run_main(self, helper, settings: Path, client) -> int:
        boto3 = types.ModuleType("boto3")
        boto3.client = mock.Mock(return_value=client)
        botocore = types.ModuleType("botocore")
        botocore_config = types.ModuleType("botocore.config")
        botocore_config.Config = lambda **kwargs: kwargs
        env = {"CRYPTO_HUNTER_TOKEN_PARAMETER": TOKEN_PARAMETER, "CRYPTO_HUNTER_SETTINGS_FILE": str(settings)}
        modules = {"boto3": boto3, "botocore": botocore, "botocore.config": botocore_config}
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, modules), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return helper.main()

    def test_reads_the_named_parameter_with_decryption(self) -> None:
        helper = load_helper()
        _, settings = self._config_with_overrides(None)
        client = mock.Mock()
        client.get_parameter.return_value = {"Parameter": {"Value": "from-ssm"}}
        self.assertEqual(self._run_main(helper, settings, client), 0)
        client.get_parameter.assert_called_once_with(Name=TOKEN_PARAMETER, WithDecryption=True)
        self.assertEqual(json.loads(settings.read_text())["web"]["api_token"], "from-ssm")

    def test_fails_closed_when_the_parameter_cannot_be_read(self) -> None:
        helper = load_helper()
        _, settings = self._config_with_overrides(None)
        client = mock.Mock()
        client.get_parameter.side_effect = RuntimeError("AccessDeniedException")
        self.assertEqual(self._run_main(helper, settings, client), 1, "systemd must see a failure and not start the engine")
        self.assertFalse(settings.exists())

    def test_refuses_to_run_unconfigured(self) -> None:
        helper = load_helper()
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("sys.stderr"):
            for name in ("CRYPTO_HUNTER_TOKEN_PARAMETER", "CRYPTO_HUNTER_SETTINGS_FILE"):
                os.environ.pop(name, None)
            self.assertEqual(helper.main(), 2)


if __name__ == "__main__":
    unittest.main()
