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
        self.assertTrue({"litellm", "open-webui"}.isdisjoint(self.config["services"]))

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

    def test_conversation_routes_use_available_ovms_by_default(self):
        routes = json.loads(self.config["services"]["mesh-orchestrator"]["environment"]["MODEL_ROUTES"])
        for alias in ("chat", "deep", "worker", "worker-big", "arc"):
            with self.subTest(alias=alias):
                self.assertEqual(routes[alias]["base_url"], "http://ia-gpu-intel-openvino:8000/v1")
                self.assertEqual(routes[alias]["model"], "Qwen3-30B-A3B-Instruct-2507-int4-ov")

    def test_retired_launch_files_removed(self):
        self.assertFalse((ROOT / "litellm/config.yaml").exists())
        self.assertFalse((ROOT / "openwebui/tools/mesh_delegate.py").exists())
        self.assertFalse(list(ROOT.glob("docker-compose.yml.bak*")))

    def test_shared_services_and_volumes_remain(self):
        for name in ("open-terminal", "searxng", "searxng-mcp", "searxng-mcp-http", "mesh-orchestrator", "ia-gpu-intel-openvino"):
            self.assertIn(name, self.config["services"])
        for volume in ("open-terminal-data", "mesh-orchestrator-data", "ov_cache"):
            self.assertIn(volume, self.config["volumes"])
        self.assertNotIn("open-webui-data", self.config.get("volumes", {}))


if __name__ == "__main__":
    unittest.main()
