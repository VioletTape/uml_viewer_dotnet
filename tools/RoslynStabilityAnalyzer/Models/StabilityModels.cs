using System.Text.Json.Serialization;

namespace RoslynStabilityAnalyzer.Models;

public class AnalysisResult
{
    [JsonPropertyName("integration_points")]
    public List<IntegrationPoint> IntegrationPoints { get; set; } = new();

    [JsonPropertyName("resilience_pipelines")]
    public List<ResiliencePipeline> ResiliencePipelines { get; set; } = new();

    [JsonPropertyName("violations")]
    public List<StabilityViolation> Violations { get; set; } = new();
}

public class IntegrationPoint
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = string.Empty;

    [JsonPropertyName("type")]
    public string Type { get; set; } = string.Empty;

    [JsonPropertyName("kind")]
    public string Kind { get; set; } = string.Empty;

    [JsonPropertyName("file")]
    public string File { get; set; } = string.Empty;

    [JsonPropertyName("line")]
    public int Line { get; set; }

    [JsonPropertyName("class_name")]
    public string ClassName { get; set; } = string.Empty;

    [JsonPropertyName("member_name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? MemberName { get; set; }

    [JsonPropertyName("has_explicit_timeout")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? HasExplicitTimeout { get; set; }
}

public class ResiliencePipeline
{
    [JsonPropertyName("strategy_type")]
    public string StrategyType { get; set; } = string.Empty;

    [JsonPropertyName("pipeline_name")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? PipelineName { get; set; }

    [JsonPropertyName("file")]
    public string File { get; set; } = string.Empty;

    [JsonPropertyName("line")]
    public int Line { get; set; }

    [JsonPropertyName("class_name")]
    public string ClassName { get; set; } = string.Empty;

    [JsonPropertyName("method_name")]
    public string MethodName { get; set; } = string.Empty;

    [JsonPropertyName("has_jitter")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? HasJitter { get; set; }

    [JsonPropertyName("backoff_type")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? BackoffType { get; set; }

    [JsonPropertyName("timeout_value")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? TimeoutValue { get; set; }

    [JsonPropertyName("handled_exceptions")]
    public List<string> HandledExceptions { get; set; } = new();
}

public class StabilityViolation
{
    [JsonPropertyName("kind")]
    public string Kind { get; set; } = string.Empty;

    [JsonPropertyName("file")]
    public string File { get; set; } = string.Empty;

    [JsonPropertyName("line")]
    public int Line { get; set; }

    [JsonPropertyName("class_name")]
    public string ClassName { get; set; } = string.Empty;

    [JsonPropertyName("method_name")]
    public string MethodName { get; set; } = string.Empty;

    [JsonPropertyName("message")]
    public string Message { get; set; } = string.Empty;

    [JsonPropertyName("severity")]
    public string Severity { get; set; } = "warning";
}
