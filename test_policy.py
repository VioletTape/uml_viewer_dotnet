#!/usr/bin/env python3
"""
test_policy.py - Tests Clean Architecture policy evaluation against OrgStructure project.
"""

from extractor import CodeGraphExtractor
from policy import ArchitecturePolicy

def main():
    project = "/home/vt/projects/organizations"
    print(f"Extracting and evaluating architecture for: {project}")

    ext = CodeGraphExtractor(project, prefix="OrgStructure")
    raw_graph = ext.extract()

    # Define policy matching OrgStructure clean architecture
    policy = ArchitecturePolicy({
        "name": "OrgStructure Clean Architecture",
        "prefix": "OrgStructure",
        "layers": [
            {"name": "Domain", "pattern": r"(^|\.)Domain(\.|$)", "rank": 0},
            {"name": "Contracts", "pattern": r"(^|\.)Contracts(\.|$)", "rank": 1},
            {"name": "Infrastructure", "pattern": r"(^|\.)Infrastructure(\.|$)", "rank": 2},
            {"name": "Api", "pattern": r"(^|\.)Api(\.|$)", "rank": 3},
            {"name": "Client", "pattern": r"(^|\.)Client(\.|$)", "rank": 4}
        ]
    })

    evaluated = policy.evaluate_graph(raw_graph)

    stats = evaluated["stats"]
    print("\n--- ARCHITECTURE SUMMARY ---")
    print(f"Total Classes/Interfaces: {stats['total_classes']}")
    print(f"Total Dependencies: {stats['total_edges']}")
    print(f"Clean Architecture Violations: {stats['total_violations']}")

    print("\nClasses per Layer:")
    for layer, count in stats['layers'].items():
        print(f"  * {layer}: {count} types")

    if evaluated["violations"]:
        print(f"\n--- VIOLATIONS FOUND ({len(evaluated['violations'])}) ---")
        for v in evaluated["violations"][:10]:
            print(f"  [X] {v['from_class']} ({v['from_layer']}) -> {v['to_class']} ({v['to_layer']})")
            print(f"      Reason: {v['reason']}")
            print(f"      Location: {v['file_path']}:{v['line']}")
    else:
        print("\nAll dependencies respect Clean Architecture rules! (Zero violations)")

if __name__ == "__main__":
    main()
