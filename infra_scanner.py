"""
infra_scanner.py - Scans deployment and infrastructure manifests for replica counts,
environments, and timeout configurations.
Parses override.yaml, override.*.yaml, workload.yaml, docker-compose.yml.
"""

import glob
import os
import re
from typing import Any, Dict, List, Optional, Tuple


def _parse_simple_yaml(content: str) -> Dict[str, Any]:
    """Lightweight fallback YAML parser for flat/nested key-value pairs without requiring PyYAML."""
    result: Dict[str, Any] = {}
    lines = content.splitlines()
    stack: List[Tuple[int, Dict[str, Any]]] = [(-1, result)]

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        match = re.match(r"^([a-zA-Z0-9_\-\.]+)\s*:\s*(.*)$", stripped)
        if match:
            key, val = match.group(1), match.group(2).strip()
            # Clean up stack to find parent
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]

            if not val:
                new_dict: Dict[str, Any] = {}
                parent[key] = new_dict
                stack.append((indent, new_dict))
            else:
                # Strip quotes if string
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                elif val.isdigit():
                    val = int(val)
                elif re.match(r"^\d+\.\d+$", val):
                    val = float(val)
                elif val.lower() == "true":
                    val = True
                elif val.lower() == "false":
                    val = False
                parent[key] = val

    return result


def parse_yaml_file(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except ImportError:
            with open(path, "r", encoding="utf-8") as f:
                return _parse_simple_yaml(f.read())
    except Exception:
        return {}


class InfraProfile:
    def __init__(self, project_path: str):
        self.project_path = os.path.abspath(project_path)
        self.max_replicas = 1
        self.environments: List[str] = []
        self.replica_by_env: Dict[str, int] = {}
        self.healthcheck_timeout_seconds: Optional[int] = None
        self.manifest_files: List[str] = []
        self._scan()

    def _scan(self):
        # 1. Look for override.yaml and override.*.yaml
        override_files = glob.glob(os.path.join(self.project_path, "override*.yaml"))
        override_files.extend(glob.glob(os.path.join(self.project_path, "override*.yml")))

        for fpath in override_files:
            rel = os.path.relpath(fpath, self.project_path)
            self.manifest_files.append(rel)
            data = parse_yaml_file(fpath)
            replicas = data.get("replicaCount")
            if isinstance(replicas, int) and replicas > 0:
                env_match = re.search(r"override\.([a-zA-Z0-9_\-]+)\.ya?ml", os.path.basename(fpath))
                env_name = env_match.group(1) if env_match else "default"
                self.replica_by_env[env_name] = replicas
                if replicas > self.max_replicas:
                    self.max_replicas = replicas

        # 2. Look for workload.yaml
        workload_path = os.path.join(self.project_path, "workload.yaml")
        if os.path.isfile(workload_path):
            self.manifest_files.append("workload.yaml")
            wdata = parse_yaml_file(workload_path)
            deps = wdata.get("deployments", {}).get("kubernetes", {})
            for _, svc_cfg in deps.items():
                if isinstance(svc_cfg, dict):
                    envs = svc_cfg.get("environments", [])
                    if isinstance(envs, list):
                        self.environments.extend(envs)
                    hc = svc_cfg.get("healthcheck", {})
                    if isinstance(hc, dict) and "timeoutSeconds" in hc:
                        self.healthcheck_timeout_seconds = hc["timeoutSeconds"]

        # Deduplicate environments
        self.environments = sorted(list(set(self.environments)))

    def is_multi_instance(self) -> bool:
        """Returns True if any environment runs >= 2 instances."""
        return self.max_replicas > 1 or any(r > 1 for r in self.replica_by_env.values())

    def get_summary(self) -> Dict[str, Any]:
        return {
            "max_replicas": self.max_replicas,
            "replica_by_env": self.replica_by_env,
            "is_multi_instance": self.is_multi_instance(),
            "environments": self.environments,
            "healthcheck_timeout_seconds": self.healthcheck_timeout_seconds,
            "manifest_files": self.manifest_files
        }
