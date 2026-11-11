"""No network/Docker required: exercise bootstrap against real local Git repos."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("init-agent.sh")


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class InitAgentTest(unittest.TestCase):
    def test_ref_must_be_pinned(self):
        self.assertTrue(SCRIPT.is_file())
        env = dict(os.environ, HERMES_AGENT_REF="main")
        result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full commit SHA", result.stderr)

    def test_recursive_checkout_can_restart_without_network(self):
        self.assertTrue(SCRIPT.is_file(), "recursive Agent initializer is missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "nested"
            agent = root / "agent"
            for repo in (nested, agent):
                repo.mkdir()
                git(repo, "init", "-q")
                git(repo, "config", "user.name", "Test")
                git(repo, "config", "user.email", "test@example.invalid")
                (repo / "pyproject.toml").write_text("# test fixture\n")
                git(repo, "add", ".")
                git(repo, "commit", "-qm", "Initial")
            git(agent, "-c", "protocol.file.allow=always", "submodule", "add", str(nested), "vendor/nested")
            git(agent, "commit", "-qam", "Nested dependency")
            ref = git(agent, "rev-parse", "HEAD")
            target = root / "checkout"
            env = dict(os.environ, HERMES_AGENT_REPO=str(agent), HERMES_AGENT_REF=ref,
                       HERMES_AGENT_TARGET=str(target), GIT_ALLOW_PROTOCOL="file",
                       WANTED_UID=str(os.getuid()), WANTED_GID=str(os.getgid()))
            subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
            self.assertEqual(git(target, "rev-parse", "HEAD"), ref)
            self.assertTrue((target / "vendor/nested/pyproject.toml").is_file())
            agent.rename(root / "offline")
            subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
            self.assertEqual(git(target, "rev-parse", "HEAD"), ref)


if __name__ == "__main__":
    unittest.main()
