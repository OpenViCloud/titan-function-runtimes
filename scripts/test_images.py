#!/usr/bin/env python3
"""Guard: what the three runtime images and the workflow that publishes them must keep true.

  - every FROM is linux/amd64 and pinned by digest (a tag moves; a digest does not);
  - every image runs as user 1000 and starts one entrypoint, the adapter (Go: the customer's bootstrap);
  - the package a runtime is published as names the version its base image has (python3.15 <-> python:3.15-slim);
  - the push builds one single-platform manifest (`provenance: false`): the composer in the function cluster refuses an index;
  - the workflow pushes only from main.

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

    def test_the_push_is_one_manifest_and_only_from_main(self):
        ci = read(".github", "workflows", "ci.yml")
        push = ci[ci.index("\n  push:"):]
        self.assertIn("provenance: false", push)
        self.assertIn("platforms: linux/amd64", push)
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", push)
        self.assertEqual(ci.count("push: true"), 1)

    def test_the_go_image_has_no_toolchain(self):
        text = read("go", "Dockerfile")
        self.assertNotIn("golang", text)
        self.assertIn("distroless/static", text)


if __name__ == "__main__":
    unittest.main()
