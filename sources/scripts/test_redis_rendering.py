#!/usr/bin/env python3
"""Redis authentication regression tests. Run with Python 3, PyYAML and Helm:

    python3 sources/scripts/test_redis_rendering.py
"""

import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


CHART = Path(__file__).resolve().parents[2]
AUTH_MODES = {"none": "ignored-user", "password": "", "acl": "app-user"}


def render(values):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as overrides:
        json.dump(values, overrides)
        overrides.flush()
        return subprocess.run(
            ["helm", "template", "redis-auth-test", str(CHART), "-f", overrides.name],
            text=True, capture_output=True, check=False,
        )


def environment_lists(value):
    if isinstance(value, dict):
        if "env" in value:
            yield value["env"]
        for child in value.values():
            yield from environment_lists(child)
    elif isinstance(value, list):
        for child in value:
            yield from environment_lists(child)


class RedisAuthenticationRendering(unittest.TestCase):
    def documents(self, values):
        result = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_username_defaults(self):
        defaults = yaml.safe_load((CHART / "values.yaml").read_text())["connections"]
        self.assertEqual(defaults["redisUser"], "default")
        self.assertEqual(defaults["redisSentinelUser"], "")
        documents = self.documents({})
        config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
        self.assertEqual(config["REDIS_SERVER_USER"], "default")
        self.assertNotIn("REDIS_SENTINEL_USER", config)

    def test_authentication_matrix(self):
        for topology, node_mode, source in itertools.product(
            ("standalone", "sentinel", "cluster"), AUTH_MODES, ("inline", "existing")
        ):
            sentinel_modes = AUTH_MODES if topology == "sentinel" else ("none",)
            for sentinel_mode in sentinel_modes:
                with self.subTest(topology=topology, node=node_mode, sentinel=sentinel_mode, source=source):
                    connections = {}
                    for prefix, mode, password in (
                        ("redis", node_mode, "node-password"),
                        ("redisSentinel", sentinel_mode, "sentinel-password"),
                    ):
                        connections.update({
                            prefix + "User": AUTH_MODES[mode],
                            prefix + "NoPass": mode == "none",
                            prefix + "Password": password if source == "inline" else "",
                            prefix + "ExistingSecret": "external-" + prefix,
                        })
                    if topology == "sentinel":
                        connections["redisSentinelNodes"] = ["sentinel-a:26379", "sentinel-b:26379"]
                    elif topology == "cluster":
                        connections["redisClusterNodes"] = ["redis-a:6379", "redis-b:6379"]
                    documents = self.documents({
                        "connections": connections,
                        "docservice": {"editorDataStorage": topology},
                    })
                    config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
                    secrets = {doc["metadata"]["name"]: doc for doc in documents if doc["kind"] == "Secret"}
                    for env_prefix, prefix, mode, password in (
                        ("REDIS_SERVER", "redis", node_mode, "node-password"),
                        ("REDIS_SENTINEL", "redisSentinel", sentinel_mode, "sentinel-password"),
                    ):
                        enabled = mode != "none" and (prefix == "redis" or topology == "sentinel")
                        if enabled and AUTH_MODES[mode]:
                            self.assertEqual(config[env_prefix + "_USER"], AUTH_MODES[mode])
                        else:
                            self.assertNotIn(env_prefix + "_USER", config)
                        refs = [entry["valueFrom"]["secretKeyRef"]
                                for env in environment_lists(documents)
                                for entry in env if entry["name"] == env_prefix + "_PWD"]
                        self.assertEqual(bool(refs), enabled)
                        created_name = "redis-auth-test-" + ("redis" if prefix == "redis" else "redis-sentinel")
                        self.assertEqual(created_name in secrets, enabled and source == "inline")
                        for ref in refs:
                            if source == "existing":
                                self.assertEqual(ref["name"], "external-" + prefix)
                            else:
                                self.assertEqual(ref["name"], created_name)
                                self.assertEqual(secrets[ref["name"]]["stringData"][ref["key"]], password)
                    for kind in ("Pod", "ConfigMap"):
                        self.assertTrue(any(doc["kind"] == kind and doc["metadata"]["name"] == "test-redis-config"
                                            for doc in documents))

    def test_missing_required_passwords(self):
        for topology, username in itertools.product(("standalone", "sentinel", "cluster"), ("", "app-user")):
            connections = {"redisUser": username, "redisExistingSecret": "", "redisPassword": ""}
            if topology == "sentinel":
                connections["redisSentinelNodes"] = ["sentinel:26379"]
            elif topology == "cluster":
                connections["redisClusterNodes"] = ["redis:6379"]
            with self.subTest(topology=topology, username=username):
                result = render({"connections": connections, "docservice": {"editorDataStorage": topology}})
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "Authenticated Redis requires connections.redisPassword or connections.redisExistingSecret|A Redis Password is required!")
        for node_no_pass, username in itertools.product((True, False), ("", "sentinel-user")):
            with self.subTest(node_no_pass=node_no_pass, sentinel_username=username):
                result = render({"connections": {
                    "redisNoPass": node_no_pass,
                    "redisSentinelNodes": ["sentinel:26379"],
                    "redisSentinelUser": username,
                    "redisSentinelNoPass": False,
                    "redisSentinelPassword": "",
                    "redisSentinelExistingSecret": "",
                }})
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "Authenticated Redis Sentinel requires connections.redisSentinelPassword or connections.redisSentinelExistingSecret|A Redis Sentinel Password is required!")

    def test_no_pass_without_passwords(self):
        self.documents({"connections": {
            "redisNoPass": True, "redisPassword": "", "redisExistingSecret": "",
            "redisSentinelNodes": ["sentinel:26379"],
            "redisSentinelNoPass": True, "redisSentinelPassword": "", "redisSentinelExistingSecret": "",
        }})

    def test_configuration_hook_selection(self):
        for storage, enabled in (("memory", True), ("standalone", False)):
            documents = self.documents({"docservice": {"editorDataStorage": storage}, "tests": {"enabled": enabled}})
            self.assertFalse(any(doc["metadata"]["name"] == "test-redis-config" for doc in documents))
        documents = self.documents({"docservice": {"editorStatStorage": "standalone"}})
        self.assertTrue(any(doc["kind"] == "Pod" and doc["metadata"]["name"] == "test-redis-config" for doc in documents))


if __name__ == "__main__":
    unittest.main()
