using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using RoslynStabilityAnalyzer.Analyzers;
using RoslynStabilityAnalyzer.Models;

namespace RoslynStabilityAnalyzer;

public static class Program
{
    public static int Main(string[] args)
    {
        var files = new List<string>();

        for (int i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            if (arg is "--files" or "--file" or "-f")
            {
                while (i + 1 < args.Length && !args[i + 1].StartsWith("--"))
                {
                    i++;
                    files.Add(Path.GetFullPath(args[i]));
                }
            }
            else if (arg is "--dir" or "-d")
            {
                while (i + 1 < args.Length && !args[i + 1].StartsWith("--"))
                {
                    i++;
                    var dir = Path.GetFullPath(args[i]);
                    if (Directory.Exists(dir))
                    {
                        var discovered = Directory.EnumerateFiles(dir, "*.cs", SearchOption.AllDirectories)
                            .Where(f => !f.Contains("/bin/") && !f.Contains("/obj/") &&
                                        !f.Contains("/.git/") && !f.Contains("/TestResults/") &&
                                        !f.Contains("\\bin\\") && !f.Contains("\\obj\\") &&
                                        !f.Contains("\\.git\\") && !f.Contains("\\TestResults\\"))
                            .Where(f => !IsTestPath(f, dir))
                            .Select(Path.GetFullPath);
                        files.AddRange(discovered);
                    }
                    else
                    {
                        Console.Error.WriteLine($"[StabilityAnalyzer] Directory not found: {dir}");
                    }
                }
            }
            else if (arg is "--help" or "-h")
            {
                Console.Error.WriteLine("RoslynStabilityAnalyzer - C# Static Stability & Resilience Analyzer");
                Console.Error.WriteLine("Usage: dotnet run --project tools/RoslynStabilityAnalyzer -- [--files <path1> <path2>...] [--dir <directory>]");
                return 0;
            }
            else if (!arg.StartsWith("--") && File.Exists(arg))
            {
                files.Add(Path.GetFullPath(arg));
            }
        }

        var engine = new StabilityAnalyzerEngine();
        var result = engine.AnalyzeFiles(files.Distinct());

        var jsonOptions = new JsonSerializerOptions
        {
            WriteIndented = true
        };

        string json = JsonSerializer.Serialize(result, jsonOptions);
        Console.WriteLine(json);

        return 0;
    }

    public static bool IsTestPath(string filePath, string? projectDir = null)
    {
        if (string.IsNullOrWhiteSpace(filePath)) return false;
        var normFile = filePath.Replace('\\', '/');

        if (!string.IsNullOrWhiteSpace(projectDir))
        {
            try
            {
                var rel = Path.GetRelativePath(projectDir, filePath).Replace('\\', '/');
                if (!rel.StartsWith(".."))
                {
                    var segments = rel.Split('/', StringSplitOptions.RemoveEmptyEntries);
                    for (int j = 0; j < segments.Length - 1; j++)
                    {
                        if (IsTestFolderName(segments[j])) return true;
                    }
                    return false;
                }
            }
            catch { }
        }

        var parts = normFile.Split('/', StringSplitOptions.RemoveEmptyEntries);
        int take = Math.Min(4, Math.Max(0, parts.Length - 1));
        for (int j = parts.Length - 1 - take; j < parts.Length - 1; j++)
        {
            if (j >= 0 && IsTestFolderName(parts[j])) return true;
        }

        return false;
    }

    private static bool IsTestFolderName(string name)
    {
        var n = name.ToLowerInvariant().Trim();
        if (n is "test" or "tests" or "unittest" or "unittests" or "integrationtest" or "integrationtests")
            return true;
        var dots = n.Split('.');
        if (dots.Any(p => p is "test" or "tests" or "unittest" or "unittests" or "integrationtest" or "integrationtests"))
            return true;
        if (n.StartsWith("test-") || n.StartsWith("test_") || n.StartsWith("tests-") || n.StartsWith("tests_"))
            return true;
        return false;
    }
}
