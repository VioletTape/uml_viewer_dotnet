"""
test_metrics.py - Quick test for QualityMetricsEngine
"""

from extractor import CodeGraphExtractor
from policy import ArchitecturePolicy
from metrics import QualityMetricsEngine

def main():
    proj = "/home/vt/projects/organizations"
    ext = CodeGraphExtractor(proj, prefix="OrgStructure")
    raw = ext.extract()
    policy = ArchitecturePolicy()
    evaluated = policy.evaluate_graph(raw)

    metrics = QualityMetricsEngine(proj)
    enriched = metrics.enrich_graph_with_metrics(evaluated)

    qs = enriched.get("quality_summary", {})
    print("\nQuality Summary:")
    print(f"  Coverage File Found: {qs.get('has_coverage_data')} ({qs.get('coverage_file')})")
    print(f"  Green Classes:  {qs.get('green_count')} ({qs.get('green_pct')}%)")
    print(f"  Yellow Classes: {qs.get('yellow_count')} ({qs.get('yellow_pct')}%)")
    print(f"  Red (CRAPpy):   {qs.get('red_count')} ({qs.get('red_pct')}%)")
    print(f"  Average CRAP:   {qs.get('avg_crap')}")

    print("\nTop 5 Most Complex / Crappy Classes:")
    sorted_by_crap = sorted(enriched["classes"], key=lambda c: c.get("crap", 0), reverse=True)
    for c in sorted_by_crap[:5]:
        print(f"  - {c['name']} ({c['layer']}): CRAP={c['crap']} (Risk: {c['risk']}, Complexity={c['complexity']}, Coverage={c['coverage_pct']}%)")
        for m in c.get("members", [])[:3]:
            print(f"      method {m['name']}: Comp={m['complexity']}, CRAP={m['crap']}, Risk={m['risk']}")

if __name__ == "__main__":
    main()
