using System;
using System.Collections.Generic;
using System.IO;
using Microsoft.CodeAnalysis.CSharp;
using RoslynStabilityAnalyzer.Models;

namespace RoslynStabilityAnalyzer.Analyzers;

public class StabilityAnalyzerEngine
{
    public AnalysisResult AnalyzeFiles(IEnumerable<string> filePaths)
    {
        var result = new AnalysisResult();

        foreach (var path in filePaths)
        {
            if (!File.Exists(path))
            {
                Console.Error.WriteLine($"[StabilityAnalyzer] File not found: {path}");
                continue;
            }

            try
            {
                var code = File.ReadAllText(path);
                var tree = CSharpSyntaxTree.ParseText(
                    code,
                    CSharpParseOptions.Default.WithLanguageVersion(LanguageVersion.Latest),
                    path: Path.GetFullPath(path)
                );

                var root = tree.GetRoot();
                var walker = new RoslynStabilityWalker(path, result);
                walker.Visit(root);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[StabilityAnalyzer] Error analyzing {path}: {ex.Message}");
            }
        }

        return result;
    }
}
