#!/usr/bin/env python3
"""
test_extractor.py - Validates extraction against a real .NET project with codegraph.
"""

from extractor import CodeGraphExtractor

def main():
    project = "/home/vt/projects/organizations"
    print(f"Testing CodeGraphExtractor against: {project}")
    ext = CodeGraphExtractor(project, prefix="OrgStructure")
    res = ext.extract()
    print(f"Extracted {len(res['classes'])} classes/interfaces")
    print(f"Extracted {len(res['edges'])} internal edges")
    print(f"Extracted {len(res['external_packages'])} external NuGet packages")

    print("\nSample Classes:")
    for c in res['classes'][:5]:
        print(f" - [{c['kind']}] {c['name']} (ns: {c['namespace']}) members={len(c['members'])}")

    print("\nSample Edges:")
    for e in res['edges'][:5]:
        print(f" - [{e['kind']}] {e['from']} -> {e['to']}")

    print("\nSample External NuGet Packages:")
    for p in res['external_packages'][:5]:
        print(f" - {p['package']} (referenced by {p['referenced_by_count']} classes, sample types: {p['types']})")

if __name__ == "__main__":
    main()
