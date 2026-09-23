"""
metrics.py - Quality, Code Coverage, Cyclomatic Complexity & CRAP Score Engine.

Calculates Savoia & Evans' CRAP (Change Risk Anti-Pattern / Change Risk Analysis and Predictions) score:
  CRAP(m) = comp(m)^2 * (1 - cov(m))^3 + comp(m)

Parses:
- Coverlet JSON (coverage.json)
- Cobertura XML (coverage.cobertura.xml)
- OpenCover XML (coverage.opencover.xml)
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple


class QualityMetricsEngine:
    def __init__(self, project_path: str, coverage_file: Optional[str] = None):
        self.project_path = os.path.abspath(project_path)
        self.coverage_file = coverage_file or self._discover_coverage_file()
        self.coverage_files: List[str] = []
        if coverage_file and os.path.isfile(coverage_file):
            self.coverage_files = [coverage_file]
        else:
            self.coverage_files = self._discover_coverage_files()

        # line coverage: (normalized_file_path, line_number) -> hit_count
        self.line_coverage: Dict[Tuple[str, int], int] = {}
        # class coverage: class_name -> {"covered": int, "total": int, "rate": float}
        self.class_coverage: Dict[str, Dict] = {}
        # cached file contents to avoid re-reading files repeatedly
        self._file_cache: Dict[str, List[str]] = {}

        if self.coverage_files:
            for cf in self.coverage_files:
                print(f"[Metrics] Loading coverage from: {cf}")
                self._load_coverage(cf)
        else:
            print("[Metrics] No coverage file found. Complexity will be computed from source directly.")

    def _discover_coverage_file(self) -> Optional[str]:
        """Auto-discovers Coverlet or Cobertura coverage files in the project."""
        candidates = [
            "coverage.json",
            "coverage.cobertura.xml",
            "coverage.opencover.xml"
        ]
        # 1. Root level
        for c in candidates:
            p = os.path.join(self.project_path, c)
            if os.path.isfile(p):
                return p

        # 2. TestResults or bin folders
        for root, _, files in os.walk(self.project_path):
            if ".git" in root or "obj" in root or ".codegraph" in root:
                continue
            for f in files:
                f_lower = f.lower()
                if ("coverage" in f_lower or "cobertura" in f_lower or "opencover" in f_lower) and (f.endswith(".xml") or f.endswith(".json")):
                    return os.path.join(root, f)
        return None

    def _discover_coverage_files(self) -> List[str]:
        """Auto-discovers all Coverlet or Cobertura coverage files across the project."""
        found = []
        candidates = [
            "coverage.json",
            "coverage.cobertura.xml",
            "coverage.opencover.xml"
        ]
        # 1. Root level
        for c in candidates:
            p = os.path.join(self.project_path, c)
            if os.path.isfile(p):
                found.append(p)

        # 2. TestResults or bin folders
        for root, _, files in os.walk(self.project_path):
            if ".git" in root or "obj" in root or ".codegraph" in root:
                continue
            for f in files:
                f_lower = f.lower()
                if ("coverage" in f_lower or "cobertura" in f_lower or "opencover" in f_lower) and (f.endswith(".xml") or f.endswith(".json")):
                    full = os.path.join(root, f)
                    if os.path.isfile(full) and os.path.getsize(full) > 0 and full not in found:
                        found.append(full)
        return found

    def _load_coverage(self, file_path: str):
        try:
            if file_path.endswith(".json"):
                self._parse_coverlet_json(file_path)
            elif file_path.endswith(".xml"):
                self._parse_coverage_xml(file_path)
        except Exception as e:
            print(f"[Metrics] Warning: Failed to parse coverage file {file_path}: {e}")

    def _parse_coverlet_json(self, file_path: str):
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for module_name, classes in data.items():
            for class_name, methods in classes.items():
                short_class = class_name.split(".")[-1]
                covered = 0
                total = 0
                for method_name, details in methods.items():
                    lines = details.get("Lines", {})
                    for line_str, hits in lines.items():
                        total += 1
                        if hits > 0:
                            covered += 1
                rate = (covered / total) if total > 0 else 0.0
                self.class_coverage[class_name] = {"covered": covered, "total": total, "rate": rate}
                self.class_coverage[short_class] = self.class_coverage[class_name]

    def _parse_coverage_xml(self, file_path: str):
        tree = ET.parse(file_path)
        root = tree.getroot()

        # Cobertura format (<coverage ...><packages>...)
        if root.tag == "coverage":
            sources = [s.text.strip() for s in root.iter("source") if s.text]
            for cls_el in root.iter("class"):
                c_name = cls_el.get("name", "")
                f_path = cls_el.get("filename", "")
                line_rate = float(cls_el.get("line-rate", "0"))
                short_class = c_name.split(".")[-1]

                abs_f_path = f_path
                if not os.path.isabs(abs_f_path):
                    for src in sources:
                        cand = os.path.join(src, f_path)
                        if os.path.isfile(cand):
                            abs_f_path = cand
                            break
                    if not os.path.isabs(abs_f_path):
                        cand = os.path.join(self.project_path, f_path)
                        if os.path.isfile(cand):
                            abs_f_path = cand

                covered_lines = 0
                valid_lines = 0
                for line_el in cls_el.iter("line"):
                    try:
                        num = int(line_el.get("number", "0"))
                        hits = int(line_el.get("hits", "0"))
                        valid_lines += 1
                        if hits > 0:
                            covered_lines += 1
                        norm_key = (self._norm_path(abs_f_path), num)
                        self.line_coverage[norm_key] = self.line_coverage.get(norm_key, 0) + hits
                    except ValueError:
                        pass

                if c_name not in self.class_coverage:
                    self.class_coverage[c_name] = {"rate": line_rate, "covered": covered_lines, "total": valid_lines}
                else:
                    prev = self.class_coverage[c_name]
                    new_cov = prev.get("covered", 0) + covered_lines
                    new_tot = prev.get("total", 0) + valid_lines
                    rate = (new_cov / new_tot) if new_tot > 0 else line_rate
                    self.class_coverage[c_name] = {"rate": rate, "covered": new_cov, "total": new_tot}
                self.class_coverage[short_class] = self.class_coverage[c_name]

        # OpenCover format (<CoverageSession>...)
        elif root.tag == "CoverageSession":
            file_map = {}
            for f_el in root.iter("File"):
                fid = f_el.get("uid")
                fpath = f_el.get("fullPath")
                if fid and fpath:
                    file_map[fid] = fpath

            for cls_el in root.iter("Class"):
                cls_name = cls_el.findtext("FullName") or ""
                summary = cls_el.find("Summary")
                if summary is not None:
                    visited = int(summary.get("visitedSequencePoints", "0"))
                    total = int(summary.get("numSequencePoints", "0"))
                    rate = (visited / total) if total > 0 else 0.0
                    short_class = cls_name.split(".")[-1]
                    self.class_coverage[cls_name] = {"rate": rate, "covered": visited, "total": total}
                    self.class_coverage[short_class] = self.class_coverage[cls_name]

    def _norm_path(self, fpath: str) -> str:
        return os.path.normpath(fpath).lower()

    def _get_file_lines(self, abs_path: str) -> List[str]:
        if abs_path not in self._file_cache:
            if os.path.isfile(abs_path):
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        self._file_cache[abs_path] = f.readlines()
                except Exception:
                    self._file_cache[abs_path] = []
            else:
                self._file_cache[abs_path] = []
        return self._file_cache[abs_path]

    def calculate_method_complexity(self, abs_file_path: str, start_line: int, end_line: int) -> int:
        """
        Computes McCabe Cyclomatic Complexity for a C# method:
        Starts at 1, increments for each decision point (if, while, for, foreach, case, catch, &&, ||, ?, => in switch).
        """
        lines = self._get_file_lines(abs_file_path)
        if not lines or start_line < 1 or end_line < start_line:
            return 1

        method_lines = lines[start_line - 1 : min(end_line, len(lines))]
        code = "".join(method_lines)

        # 1. Remove comments
        code = re.sub(r"//.*", "", code)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)

        # 2. Remove string literals to avoid counting keywords in strings
        code = re.sub(r'@"([^"]|"")*"', '""', code)
        code = re.sub(r'"([^"\\]|\\.)*"', '""', code)

        complexity = 1

        # Control flow keywords
        complexity += len(re.findall(r"\bif\b", code))
        complexity += len(re.findall(r"\bwhile\b", code))
        complexity += len(re.findall(r"\bfor\b", code))
        complexity += len(re.findall(r"\bforeach\b", code))
        complexity += len(re.findall(r"\bcase\b", code))
        complexity += len(re.findall(r"\bcatch\b", code))

        # Boolean and conditional operators
        complexity += len(re.findall(r"&&", code))
        complexity += len(re.findall(r"\|\|", code))
        complexity += len(re.findall(r"\?\?", code))
        # Ternary operator (? not followed by . or ?)
        complexity += len(re.findall(r"(?<!\?)\?(?!\.|\?|:)", code))
        # Switch expression arms (=>)
        if "switch" in code:
            complexity += max(0, len(re.findall(r"=>", code)) - 1)

        return complexity

    def calculate_crap(self, complexity: int, coverage_rate: float) -> float:
        """
        CRAP(m) = comp(m)^2 * (1 - cov(m))^3 + comp(m)
        """
        cov = max(0.0, min(1.0, coverage_rate))
        crap = (complexity ** 2) * ((1.0 - cov) ** 3) + complexity
        return round(crap, 1)

    def get_crap_risk_level(self, crap_score: float) -> str:
        """Categorizes CRAP score into traffic light risk levels."""
        if crap_score <= 15.0:
            return "green"   # Low risk / well-covered
        elif crap_score <= 30.0:
            return "yellow"  # Medium risk / needs testing
        else:
            return "red"     # High risk / Alberto Savoia CRAP threshold exceeded!

    def enrich_graph_with_metrics(self, graph_data: Dict) -> Dict:
        """
        Enriches classes and methods in graph_data with coverage, complexity, CRAP scores, and risk ratings.
        """
        classes = graph_data.get("classes", [])
        overall_crap_scores = []
        green_count = 0
        yellow_count = 0
        red_count = 0

        for cls in classes:
            c_name = cls.get("name", "")
            q_name = cls.get("qualified_name", "")
            file_path = cls.get("file_path", "")
            abs_path = file_path if os.path.isabs(file_path) else os.path.join(self.project_path, file_path)

            # Class coverage
            cov_info = self.class_coverage.get(q_name) or self.class_coverage.get(c_name)
            class_cov_rate = cov_info["rate"] if cov_info else 0.0

            if not class_cov_rate:
                norm_p = self._norm_path(abs_path)
                file_lines = [h for (p, l), h in self.line_coverage.items() if p == norm_p]
                if file_lines:
                    cov_cnt = sum(1 for h in file_lines if h > 0)
                    class_cov_rate = cov_cnt / len(file_lines)

            total_comp = 0
            max_crap = 0.0
            enriched_members = []

            for m in cls.get("members", []):
                s_line = m.get("start_line") or m.get("line") or 1
                e_line = m.get("end_line") or s_line

                # Compute complexity from C# source code
                comp = self.calculate_method_complexity(abs_path, s_line, e_line)
                total_comp += comp

                # Method coverage (fallback to class coverage if line-specific not available)
                m_cov_rate = class_cov_rate
                # Check line coverage if available
                norm_p = self._norm_path(abs_path)
                m_lines = [self.line_coverage.get((norm_p, l)) for l in range(s_line, e_line + 1) if (norm_p, l) in self.line_coverage]
                if m_lines:
                    covered_l = sum(1 for h in m_lines if h > 0)
                    m_cov_rate = covered_l / len(m_lines)

                crap = self.calculate_crap(comp, m_cov_rate)
                risk = self.get_crap_risk_level(crap)

                if crap > max_crap:
                    max_crap = crap

                enriched_members.append({
                    **m,
                    "complexity": comp,
                    "coverage_pct": round(m_cov_rate * 100, 1),
                    "crap": crap,
                    "risk": risk
                })

            cls["members"] = enriched_members
            cls["complexity"] = total_comp
            cls["coverage_pct"] = round(class_cov_rate * 100, 1)

            # Class CRAP rating is dictated by its highest risk method or class-level formula
            class_crap = max_crap if max_crap > 0 else self.calculate_crap(max(1, total_comp), class_cov_rate)
            cls["crap"] = class_crap
            class_risk = self.get_crap_risk_level(class_crap)
            cls["risk"] = class_risk

            overall_crap_scores.append(class_crap)
            if class_risk == "green":
                green_count += 1
            elif class_risk == "yellow":
                yellow_count += 1
            else:
                red_count += 1

        total_classes = len(classes)
        graph_data["quality_summary"] = {
            "has_coverage_data": bool(self.coverage_files),
            "coverage_files": [os.path.relpath(f, self.project_path) for f in self.coverage_files],
            "coverage_file": os.path.relpath(self.coverage_files[0], self.project_path) if self.coverage_files else None,
            "green_count": green_count,
            "yellow_count": yellow_count,
            "red_count": red_count,
            "green_pct": round((green_count / total_classes * 100), 1) if total_classes else 0,
            "yellow_pct": round((yellow_count / total_classes * 100), 1) if total_classes else 0,
            "red_pct": round((red_count / total_classes * 100), 1) if total_classes else 0,
            "avg_crap": round(sum(overall_crap_scores) / total_classes, 1) if total_classes else 0
        }

        return graph_data
