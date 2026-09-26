"""
stability_store.py - Persistent atomic cache for stability pattern findings.
Stores findings in .uml-viewer/stability_findings.json and cache hashes in
.uml-viewer/stability_cache.json.
Ensures zero token waste on page reloads or 'uml' start if source files have not changed.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mailbox_store import mailbox
from path_utils import is_test_path


def compute_file_hash(path: str) -> Optional[str]:
    """Computes SHA256 hex digest of a file."""
    if not os.path.isfile(path):
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def compute_file_fingerprint(path: str) -> Optional[str]:
    """Computes fast stat-based fingerprint (mtime_ns:size) without reading full file bytes."""
    try:
        st = os.stat(path)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except Exception:
        return None


class StabilityStore:
    def __init__(self, project_path: str):
        self.project_path = os.path.abspath(project_path)
        self.cache_dir = os.path.join(self.project_path, ".uml-viewer")
        self.cache_file = os.path.join(self.cache_dir, "stability_cache.json")
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_relevant_files(self) -> List[str]:
        """Finds .cs files and infra manifests for hashing."""
        relevant = []
        # Infrastructure configs
        excluded_dirs = {"bin", "obj", ".git", "node_modules", ".vs", "TestResults", ".codegraph", ".uml-viewer"}
        for root, dirs, files in os.walk(self.project_path):
            # Skip build output & vendor dirs by pruning in-place
            dirs[:] = [d for d in dirs if d not in excluded_dirs and not d.startswith(".")]
            if is_test_path(root, self.project_path):
                dirs[:] = []
                continue
            for f in files:
                if f.endswith(".cs") or f in ("override.yaml", "workload.yaml") or f.startswith("override."):
                    fpath = os.path.join(root, f)
                    if not is_test_path(fpath, self.project_path):
                        relevant.append(fpath)
        return sorted(relevant)

    def compute_project_signature(self, fast: bool = True) -> Tuple[Dict[str, str], str]:
        """
        Computes signatures for all relevant source and infra files.
        If fast=True, uses mtime_ns:size stat fingerprints avoiding reading whole files.
        Returns (file_hashes_dict, combined_signature_hash).
        """
        files = self._get_relevant_files()
        file_hashes = {}
        combined = hashlib.sha256()

        for fpath in files:
            rel = os.path.relpath(fpath, self.project_path)
            fhash = compute_file_fingerprint(fpath) if fast else compute_file_hash(fpath)
            if fhash:
                file_hashes[rel] = fhash
                combined.update(f"{rel}:{fhash}".encode("utf-8"))

        # Also include .codegraph/codegraph.db modification time if it exists
        cg_db = os.path.join(self.project_path, ".codegraph", "codegraph.db")
        if os.path.isfile(cg_db):
            try:
                st = os.stat(cg_db)
                combined.update(f"codegraph.db:{st.st_mtime_ns}:{st.st_size}".encode("utf-8"))
            except Exception:
                pass

        return file_hashes, combined.hexdigest()

    def is_cache_valid(self) -> bool:
        """Returns True if the cache matches current files on disk."""
        if not os.path.isfile(self.cache_file):
            return False

        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            cached_sig = cached_data.get("signature")
            if not cached_sig:
                return False

            _, current_sig = self.compute_project_signature()
            return current_sig == cached_sig
        except Exception:
            return False

    def get_findings(self) -> List[Dict[str, Any]]:
        """Reads persisted stability findings from mailbox."""
        with mailbox(self.project_path, "stability_findings.json") as items:
            return list(items)

    def save_findings(self, findings: List[Dict[str, Any]]):
        """Atomically saves findings to mailbox and updates cache signature."""
        with mailbox(self.project_path, "stability_findings.json") as items:
            items.clear()
            items.extend(findings)

        file_hashes, sig = self.compute_project_signature()
        cache_meta = {
            "signature": sig,
            "total_findings": len(findings),
            "file_count": len(file_hashes)
        }
        temp_file = f"{self.cache_file}.{os.getpid()}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(cache_meta, f, indent=2)
            os.replace(temp_file, self.cache_file)
        except Exception as e:
            print(f"[StabilityStore] Failed to write cache metadata: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    def invalidate_cache(self):
        """Forces cache invalidation to trigger re-evaluation."""
        if os.path.isfile(self.cache_file):
            try:
                os.remove(self.cache_file)
            except OSError:
                pass
