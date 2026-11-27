#!/usr/bin/env python3
"""Guard: what the three runtime images and the workflow that publishes them must keep true.

  - every FROM is linux/amd64 and pinned by digest (a tag moves; a digest does not);
  - every image runs as user 1000 and starts one entrypoint, the adapter (Go: the customer's bootstrap);
  - the package a runtime is published as names the version its base image has (python3.15 <-> python:3.15-slim);
  - the push builds one single-platform manifest (`provenance: false`): the composer in the function cluster refuses an index;
  - the workflow pushes only from main, once the tests and the image builds passed, and only the push job can write packages;
  - the workflow runs on pull_request (never pull_request_target) and on pushes to main, and can only read by default.

Run:  python3 scripts/test_images.py
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


RUNTIMES = {"python": "python3.15", "node": "nodejs26", "go": "go"}


class Images(unittest.TestCase):
    def test_every_base_is_amd64_and_pinned_by_digest(self):
        for d in RUNTIMES:
            froms = re.findall(r"^FROM (.+)$", read(d, "Dockerfile"), re.M)
            self.assertEqual(len(froms), 1, d)
            self.assertRegex(froms[0], r"^--platform=linux/amd64 \S+@sha256:[0-9a-f]{64}$", d)

    def test_every_image_runs_as_1000_with_one_entrypoint(self):
        for d in RUNTIMES:
            text = read(d, "Dockerfile")
            self.assertEqual(re.findall(r"^USER (.+)$", text, re.M), ["1000:1000"], d)
            self.assertEqual(len(re.findall(r"^ENTRYPOINT ", text, re.M)), 1, d)
            self.assertNotIn("CMD", text, d)

    def test_the_runtime_name_names_the_version_of_its_base(self):
        python = re.search(r"python:(\d+\.\d+)-slim@", read("python", "Dockerfile")).group(1)
        node = re.search(r"node:(\d+)-slim@", read("node", "Dockerfile")).group(1)
        self.assertEqual(RUNTIMES["python"], f"python{python}")
        self.assertEqual(RUNTIMES["node"], f"nodejs{node}")

    def test_the_workflow_publishes_each_runtime_under_its_name(self):
        ci = read(".github", "workflows", "ci.yml")
        for d, name in RUNTIMES.items():
            self.assertRegex(ci, rf"dir: {d}\s+name: {re.escape(name)}\b", d)

    def jobs(self):
        ci = read(".github", "workflows", "ci.yml")
        text = ci[ci.index("\njobs:"):]
        parts = re.split(r"\n  (?=[A-Za-z0-9_-]+:\n    name:)", text)[1:]
        return {p.split(":", 1)[0]: p for p in parts}

    def test_the_push_is_one_manifest_and_only_from_main(self):
        jobs = self.jobs()
        self.assertEqual(sorted(jobs), ["image", "push", "test"])
        push = jobs["push"]
        self.assertIn("provenance: false", push)
        self.assertIn("platforms: linux/amd64", push)
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", push)
        self.assertIn("push: true", push)

    def test_the_workflow_runs_on_pull_requests_and_pushes_to_main_only(self):
        ci = read(".github", "workflows", "ci.yml")
        on = ci[ci.index("\non:"):ci.index("\npermissions:")]
        self.assertNotIn("pull_request_target", on)
        self.assertRegex(on, r"push:\s+branches: \[main\]")
        self.assertRegex(on, r"pull_request:\s+branches: \[main\]")

    def test_the_workflow_by_default_can_only_read(self):
        ci = read(".github", "workflows", "ci.yml")
        top = ci[ci.index("\npermissions:"):ci.index("\njobs:")]
        self.assertEqual(re.findall(r"^  (\S+): (\S+)$", top, re.M), [("contents", "read")])

    def test_the_push_waits_for_the_tests_and_the_image_builds(self):
        self.assertRegex(self.jobs()["push"], r"needs: \[test, image\]")

    def test_only_the_push_job_can_write_packages_or_push(self):
        for name, job in self.jobs().items():
            if name != "push":
                self.assertNotIn("packages: write", job, name)
                self.assertNotIn("push: true", job, name)
                self.assertNotIn("docker/login-action", job, name)

    def test_the_docker_actions_are_pinned_by_commit(self):
        uses = re.findall(r"uses: (docker/\S+)", read(".github", "workflows", "ci.yml"))
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"^docker/[\w-]+@[0-9a-f]{40}$", u)

    def test_the_go_image_has_no_toolchain(self):
        text = read("go", "Dockerfile")
        self.assertNotIn("golang", text)
        self.assertIn("distroless/static", text)


if __name__ == "__main__":
    unittest.main()
