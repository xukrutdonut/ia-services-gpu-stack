"""Regression checks against the rendered Compose graph (no containers started)."""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RetiredServicesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "MOODLE_TOKEN": "validation-only"}
        result = subprocess.run(
            ["docker", "compose", "--env-file", "/dev/null", "--profile", "*",
             "config", "--format", "json"], cwd=ROOT, env=env,
            check=True, capture_output=True, text=True,
        )
        cls.config = json.loads(result.stdout)

    def test_removed_services_not_in_any_profile(self):
        removed = {
            "litellm",
            "open-webui",
            "mesh-orchestrator",
            "fetch-mcp",
            "nano-fs-tools-mcp",
            "searxng-mcp-http",
        }
        self.assertTrue(removed.isdisjoint(self.config["services"]))

    def test_all_startup_dependencies_exist(self):
        for name, service in self.config["services"].items():
            with self.subTest(service=name):
                self.assertTrue(set(service.get("depends_on", {})) <= self.config["services"].keys())

    def test_no_active_gateway_urls_or_secrets(self):
        for name, service in self.config["services"].items():
            with self.subTest(service=name):
                text = json.dumps(service)
                self.assertNotRegex(text, r"(?i)https?://(?:litellm|open-webui)(?::|/)")
                self.assertNotRegex(text, r"LITELLM_(?:BASE|MASTER_KEY)|OPENWEBUI_SECRET_KEY")

    def test_ninfer_server_runs_on_the_v100_with_host_networking(self):
        ninfer = self.config["services"]["ninfer-server"]
        self.assertEqual(ninfer["image"], "ninfer-v100:bench")
        self.assertEqual(ninfer["container_name"], "ninfer-serve")
        self.assertEqual(ninfer["network_mode"], "host")
        self.assertEqual([d["source"] for d in ninfer["devices"]], ["nvidia.com/gpu=all"])
        targets = {mount["target"] for mount in ninfer["volumes"]}
        self.assertEqual(targets, {"/models", "/grafts"})
        for mount in ninfer["volumes"]:
            self.assertTrue(mount["read_only"])
        # El motor lo arranca ninfer-serve.sh dentro del contenedor, no el Compose.
        self.assertEqual(ninfer["command"], ["sleep", "infinity"])

    def test_retired_launch_files_removed(self):
        self.assertFalse((ROOT / "litellm/config.yaml").exists())
        self.assertFalse((ROOT / "openwebui/tools/mesh_delegate.py").exists())
        self.assertFalse(list(ROOT.glob("docker-compose.yml.bak*")))

    def test_shared_services_and_volumes_remain(self):
        for name in ("open-terminal", "searxng", "searxng-mcp", "ninfer-server",
                     "ia-gpu-intel-openvino", "hermes-webui"):
            self.assertIn(name, self.config["services"])
        for volume in ("open-terminal-data", "ov_cache", "hermes-agent-src"):
            self.assertIn(volume, self.config["volumes"])
        self.assertNotIn("mesh-orchestrator-data", self.config.get("volumes", {}))
        self.assertNotIn("open-webui-data", self.config.get("volumes", {}))

    def test_hermes_starts_agent_before_webui_without_optional_profiles(self):
        webui = self.config["services"]["hermes-webui"]
        initializer = self.config["services"]["hermes-agent-init"]
        self.assertNotIn("profiles", webui)
        self.assertNotIn("profiles", initializer)
        self.assertEqual(
            webui["depends_on"]["hermes-agent-init"]["condition"],
            "service_completed_successfully",
        )

    def test_hermes_uses_pinned_agent_and_persistent_isolated_source(self):
        initializer = self.config["services"]["hermes-agent-init"]
        self.assertRegex(
            initializer["environment"]["HERMES_AGENT_REF"], r"^[0-9a-f]{40}$"
        )
        webui = self.config["services"]["hermes-webui"]
        source_mount = next(
            mount for mount in webui["volumes"]
            if mount["target"] == "/home/hermeswebui/.hermes/hermes-agent"
        )
        self.assertEqual(source_mount["type"], "volume")
        self.assertEqual(source_mount["source"], "hermes-agent-src")
        self.assertTrue(source_mount["read_only"])
        self.assertIn("hermes-agent-src", self.config["volumes"])

    def test_hermes_chat_is_in_process_not_gateway_dependent(self):
        services = self.config["services"]
        self.assertEqual(services["hermes-webui"]["network_mode"], "host")
        all_text = json.dumps(services)
        self.assertNotIn("HERMES_API_URL", all_text)
        self.assertNotIn("8788", all_text)


if __name__ == "__main__":
    unittest.main()
