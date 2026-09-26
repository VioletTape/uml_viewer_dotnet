using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using RoslynStabilityAnalyzer.Models;

namespace RoslynStabilityAnalyzer.Analyzers;

public class RoslynStabilityWalker : CSharpSyntaxWalker
{
    private readonly string _filePath;
    private readonly AnalysisResult _result;
    private readonly HashSet<string> _reportedViolations = new(StringComparer.Ordinal);

    private string _currentClass = "Program";
    private string _currentMember = "Main";
    private string? _currentMethodCtParam = null;

    private static readonly HashSet<string> MonitoredAsyncMethods = new(StringComparer.Ordinal)
    {
        "GetAsync",
        "PostAsync",
        "PutAsync",
        "DeleteAsync",
        "PatchAsync",
        "SendAsync",
        "SaveChangesAsync",
        "ExecuteAsync",
        "ExecuteScalarAsync",
        "QueryAsync",
        "GetFromJsonAsync",
        "PostAsJsonAsync",
        "PutAsJsonAsync",
        "DeleteFromJsonAsync"
    };

    public RoslynStabilityWalker(string filePath, AnalysisResult result)
    {
        _filePath = Path.GetFullPath(filePath);
        _result = result;
    }

    private int GetLineNumber(SyntaxNode node)
    {
        return node.GetLocation().GetLineSpan().StartLinePosition.Line + 1;
    }

    private void AddViolation(string kind, SyntaxNode node, string message, string severity = "warning")
    {
        int line = GetLineNumber(node);
        string key = $"{kind}:{_filePath}:{line}";
        if (_reportedViolations.Add(key))
        {
            _result.Violations.Add(new StabilityViolation
            {
                Kind = kind,
                File = _filePath,
                Line = line,
                ClassName = _currentClass,
                MethodName = _currentMember,
                Message = message,
                Severity = severity
            });
        }
    }

    public static string? ClassifyIntegrationKind(string typeName)
    {
        if (string.IsNullOrWhiteSpace(typeName)) return null;

        var cleanType = typeName.TrimEnd('?', ' ');
        if (cleanType.Contains('<'))
        {
            // E.g. DbContextPool<AppDbContext>, IHttpClientFactory
            cleanType = cleanType.Substring(0, cleanType.IndexOf('<')).Trim();
        }
        var shortType = cleanType.Contains('.') ? cleanType.Substring(cleanType.LastIndexOf('.') + 1) : cleanType;

        if (shortType is "HttpClient" or "HttpMessageInvoker" or "IHttpClientFactory")
            return "HttpClient";

        if (cleanType == "DbContext" || cleanType.EndsWith("DbContext", StringComparison.Ordinal))
            return "DbContext";

        if (cleanType is "IDbConnection" or "DbConnection" or "ISqlConnection" or "SqlConnection" or
            "NpgsqlConnection" or "SqliteConnection" or "MySqlConnection" ||
            (cleanType.EndsWith("Connection", StringComparison.Ordinal) && !cleanType.Contains("Http")))
            return "DbConnection";

        if (cleanType is "IConnectionMultiplexer" or "ConnectionMultiplexer" or "IDatabase")
            return "Redis";

        if (cleanType is "IBus" or "IPublishEndpoint" or "ISendEndpoint" or "IModel" or "IChannel")
            return "MessageBus";

        return null;
    }

    public override void VisitClassDeclaration(ClassDeclarationSyntax node)
    {
        var prevClass = _currentClass;
        _currentClass = node.Identifier.Text;
        base.VisitClassDeclaration(node);
        _currentClass = prevClass;
    }

    public override void VisitRecordDeclaration(RecordDeclarationSyntax node)
    {
        var prevClass = _currentClass;
        _currentClass = node.Identifier.Text;
        base.VisitRecordDeclaration(node);
        _currentClass = prevClass;
    }

    public override void VisitStructDeclaration(StructDeclarationSyntax node)
    {
        var prevClass = _currentClass;
        _currentClass = node.Identifier.Text;
        base.VisitStructDeclaration(node);
        _currentClass = prevClass;
    }

    public override void VisitMethodDeclaration(MethodDeclarationSyntax node)
    {
        var prevMember = _currentMember;
        var prevCt = _currentMethodCtParam;

        _currentMember = node.Identifier.Text;
        _currentMethodCtParam = node.ParameterList.Parameters
            .FirstOrDefault(p => IsCancellationTokenParameter(p))
            ?.Identifier.Text;

        // Inspect parameters for integration types
        foreach (var param in node.ParameterList.Parameters)
        {
            var pType = param.Type?.ToString() ?? "";
            var kind = ClassifyIntegrationKind(pType);
            if (kind != null)
            {
                _result.IntegrationPoints.Add(new IntegrationPoint
                {
                    Name = param.Identifier.Text,
                    Type = pType,
                    Kind = kind,
                    File = _filePath,
                    Line = GetLineNumber(param),
                    ClassName = _currentClass,
                    MemberName = _currentMember
                });
            }
        }

        // Inspect for suspicious custom resilience loops (Hardened Triad Scout)
        InspectForSuspiciousCustomResilience(node);

        base.VisitMethodDeclaration(node);

        _currentMember = prevMember;
        _currentMethodCtParam = prevCt;
    }

    public override void VisitConstructorDeclaration(ConstructorDeclarationSyntax node)
    {
        var prevMember = _currentMember;
        var prevCt = _currentMethodCtParam;

        _currentMember = node.Identifier.Text;
        _currentMethodCtParam = node.ParameterList.Parameters
            .FirstOrDefault(p => IsCancellationTokenParameter(p))
            ?.Identifier.Text;

        // Inspect constructor parameters for integration dependencies (DI injection)
        foreach (var param in node.ParameterList.Parameters)
        {
            var pType = param.Type?.ToString() ?? "";
            var kind = ClassifyIntegrationKind(pType);
            if (kind != null)
            {
                _result.IntegrationPoints.Add(new IntegrationPoint
                {
                    Name = param.Identifier.Text,
                    Type = pType,
                    Kind = kind,
                    File = _filePath,
                    Line = GetLineNumber(param),
                    ClassName = _currentClass,
                    MemberName = _currentMember
                });
            }
        }

        base.VisitConstructorDeclaration(node);

        _currentMember = prevMember;
        _currentMethodCtParam = prevCt;
    }

    public override void VisitFieldDeclaration(FieldDeclarationSyntax node)
    {
        var typeStr = node.Declaration.Type.ToString();
        InspectFieldForUnboundedQueue(node);
        var kind = ClassifyIntegrationKind(typeStr);
        if (kind != null)
        {
            foreach (var variable in node.Declaration.Variables)
            {
                _result.IntegrationPoints.Add(new IntegrationPoint
                {
                    Name = variable.Identifier.Text,
                    Type = typeStr,
                    Kind = kind,
                    File = _filePath,
                    Line = GetLineNumber(variable),
                    ClassName = _currentClass,
                    MemberName = variable.Identifier.Text
                });
            }
        }
        base.VisitFieldDeclaration(node);
    }

    public override void VisitPropertyDeclaration(PropertyDeclarationSyntax node)
    {
        var typeStr = node.Type.ToString();
        InspectPropertyForUnboundedQueue(node);
        var kind = ClassifyIntegrationKind(typeStr);
        if (kind != null)
        {
            _result.IntegrationPoints.Add(new IntegrationPoint
            {
                Name = node.Identifier.Text,
                Type = typeStr,
                Kind = kind,
                File = _filePath,
                Line = GetLineNumber(node),
                ClassName = _currentClass,
                MemberName = node.Identifier.Text
            });
        }
        base.VisitPropertyDeclaration(node);
    }

    public override void VisitObjectCreationExpression(ObjectCreationExpressionSyntax node)
    {
        var typeStr = node.Type.ToString();
        var kind = ClassifyIntegrationKind(typeStr);

        if (kind == "HttpClient")
        {
            bool hasExplicitTimeout = false;
            string? timeoutValue = null;

            // 1. Check object initializer: new HttpClient { Timeout = TimeSpan.FromSeconds(5) }
            if (node.Initializer != null)
            {
                foreach (var expr in node.Initializer.Expressions)
                {
                    if (expr is AssignmentExpressionSyntax assign &&
                        assign.Left.ToString() == "Timeout")
                    {
                        hasExplicitTimeout = true;
                        timeoutValue = ExtractTimeoutValue(assign.Right);
                        break;
                    }
                }
            }

            // 2. Check subsequent assignment in the same block/method: client.Timeout = ...
            if (!hasExplicitTimeout && node.Parent is EqualsValueClauseSyntax eq &&
                eq.Parent is VariableDeclaratorSyntax declarator)
            {
                var varName = declarator.Identifier.Text;
                var enclosingBlock = node.Ancestors().OfType<BlockSyntax>().FirstOrDefault();
                if (enclosingBlock != null)
                {
                    foreach (var assign in enclosingBlock.DescendantNodes().OfType<AssignmentExpressionSyntax>())
                    {
                        if (assign.Left is MemberAccessExpressionSyntax memberAccess &&
                            memberAccess.Expression.ToString() == varName &&
                            memberAccess.Name.Identifier.Text == "Timeout")
                        {
                            hasExplicitTimeout = true;
                            timeoutValue = ExtractTimeoutValue(assign.Right);
                            break;
                        }
                    }
                }
            }

            string assignedName = GetAssignedVariableName(node) ?? "httpClient";

            _result.IntegrationPoints.Add(new IntegrationPoint
            {
                Name = assignedName,
                Type = typeStr,
                Kind = "HttpClient",
                File = _filePath,
                Line = GetLineNumber(node),
                ClassName = _currentClass,
                MemberName = _currentMember,
                HasExplicitTimeout = hasExplicitTimeout
            });

            if (!hasExplicitTimeout)
            {
                AddViolation(
                    "default_timeout",
                    node,
                    "HttpClient is instantiated without an explicit Timeout (defaults to 100 seconds in .NET). Configure an explicit Timeout to prevent hanging socket exhaustion under remote latency.",
                    "warning"
                );
            }
        }
        else if (kind != null)
        {
            _result.IntegrationPoints.Add(new IntegrationPoint
            {
                Name = GetAssignedVariableName(node) ?? typeStr,
                Type = typeStr,
                Kind = kind,
                File = _filePath,
                Line = GetLineNumber(node),
                ClassName = _currentClass,
                MemberName = _currentMember
            });
        }
        else if (typeStr.Contains("RetryStrategyOptions") || typeStr.Contains("HttpRetryStrategyOptions"))
        {
            var creationText = node.ToString();
            if (System.Text.RegularExpressions.Regex.IsMatch(creationText, @"UseJitter\s*=\s*false", System.Text.RegularExpressions.RegexOptions.IgnoreCase))
            {
                AddViolation(
                    "unjittered_retry",
                    node,
                    "Retry strategy explicitly disables jitter (UseJitter = false). Retries across instances will synchronize and overwhelm downstreams ('Killed by the Mob' risk).",
                    "error"
                );
            }
        }

        InspectObjectCreationForUnboundedQueue(node);

        base.VisitObjectCreationExpression(node);
    }

    public override void VisitInvocationExpression(InvocationExpressionSyntax node)
    {
        string? methodName = null;
        if (node.Expression is MemberAccessExpressionSyntax memberAccess)
        {
            methodName = memberAccess.Name.Identifier.Text;
        }
        else if (node.Expression is IdentifierNameSyntax idName)
        {
            methodName = idName.Identifier.Text;
        }

        if (methodName != null)
        {
            // Inspection: DI AddHttpClient registration
            if (methodName == "AddHttpClient")
            {
                InspectAddHttpClient(node);
            }
            // Inspection: Missing CancellationToken in async integration calls
            else if (MonitoredAsyncMethods.Contains(methodName))
            {
                InspectAsyncCallForCancellationToken(node, methodName);
            }
            // Inspection: Polly Resilience Pipelines & Handlers
            else if (methodName is "AddStandardResilienceHandler" or "AddResiliencePipeline" or
                     "AddRetry" or "AddCircuitBreaker" or "AddTimeout" or "AddPolicyHandler" or
                     "WaitAndRetry" or "WaitAndRetryAsync" or "Retry" or "RetryAsync" or "Timeout" or "TimeoutAsync")
            {
                InspectResilienceCall(node, methodName);
            }
            // Inspection: Unbounded or excessively sized Channel buffers
            else if (methodName == "CreateUnbounded")
            {
                InspectChannelCreateUnbounded(node);
            }
            else if (methodName == "CreateBounded")
            {
                InspectChannelCreateBounded(node);
            }
        }

        base.VisitInvocationExpression(node);
    }

    private void InspectAddHttpClient(InvocationExpressionSyntax node)
    {
        // Extract client name
        string clientName = "HttpClient";
        var firstArg = node.ArgumentList.Arguments.FirstOrDefault();
        if (firstArg != null && firstArg.Expression is LiteralExpressionSyntax lit)
        {
            clientName = lit.Token.ValueText;
        }
        else if (node.Expression is MemberAccessExpressionSyntax ma && ma.Name is GenericNameSyntax gn)
        {
            clientName = gn.TypeArgumentList.Arguments.FirstOrDefault()?.ToString() ?? "HttpClient";
        }

        // Check if explicit timeout or resilience is configured
        bool hasTimeoutOrResilience = false;

        // 1. Check arguments of AddHttpClient for lambda configuring Timeout
        foreach (var arg in node.ArgumentList.Arguments)
        {
            if (arg.Expression is LambdaExpressionSyntax lambda)
            {
                if (lambda.ToString().Contains("Timeout") && lambda.ToString().Contains("TimeSpan"))
                {
                    hasTimeoutOrResilience = true;
                    break;
                }
            }
        }

        // 2. Check chained invocations: .ConfigureHttpClient(...), .AddStandardResilienceHandler(), .AddTimeout(), .AddPolicyHandler()
        var chain = GetInvocationChain(node);
        foreach (var call in chain)
        {
            var chainedMethod = GetMethodName(call);
            if (chainedMethod is "AddStandardResilienceHandler" or "AddTimeout" or "AddPolicyHandler" or "AddResiliencePipeline")
            {
                hasTimeoutOrResilience = true;
                break;
            }
            if (chainedMethod == "ConfigureHttpClient")
            {
                if (call.ToString().Contains("Timeout") && call.ToString().Contains("TimeSpan"))
                {
                    hasTimeoutOrResilience = true;
                    break;
                }
            }
        }

        _result.IntegrationPoints.Add(new IntegrationPoint
        {
            Name = clientName,
            Type = "HttpClient",
            Kind = "HttpClient",
            File = _filePath,
            Line = GetLineNumber(node),
            ClassName = _currentClass,
            MemberName = _currentMember,
            HasExplicitTimeout = hasTimeoutOrResilience
        });

        if (!hasTimeoutOrResilience)
        {
            AddViolation(
                "default_timeout",
                node,
                $"HttpClient registration '{clientName}' does not configure an explicit Timeout or resilience handler (defaults to 100 seconds in .NET).",
                "warning"
            );
        }
    }

    private void InspectAsyncCallForCancellationToken(InvocationExpressionSyntax node, string methodName)
    {
        var args = node.ArgumentList.Arguments;
        bool hasCt = false;

        // Check named argument first
        if (args.Any(a => a.NameColon?.Name.Identifier.Text is "cancellationToken" or "ct" or "token"))
        {
            hasCt = true;
        }
        else
        {
            switch (methodName)
            {
                case "SaveChangesAsync":
                    if (args.Count == 0)
                    {
                        hasCt = false;
                    }
                    else if (args.Count == 1 && IsBooleanLiteral(args[0].Expression))
                    {
                        hasCt = false;
                    }
                    else
                    {
                        hasCt = args.Any(IsCancellationTokenArgument);
                    }
                    break;

                case "GetAsync":
                case "DeleteAsync":
                case "DeleteFromJsonAsync":
                    // 1 arg (url) -> no CT
                    if (args.Count <= 1)
                    {
                        hasCt = false;
                    }
                    else
                    {
                        hasCt = args.Any(IsCancellationTokenArgument);
                    }
                    break;

                case "PostAsync":
                case "PutAsync":
                case "PatchAsync":
                    // 2 args (url, content) -> no CT
                    if (args.Count <= 2)
                    {
                        hasCt = false;
                    }
                    else
                    {
                        hasCt = args.Any(IsCancellationTokenArgument);
                    }
                    break;

                case "SendAsync":
                    if (args.Count <= 1)
                    {
                        hasCt = false;
                    }
                    else
                    {
                        hasCt = args.Any(IsCancellationTokenArgument);
                    }
                    break;

                default:
                    // Dapper ExecuteAsync, QueryAsync, GetFromJsonAsync, PostAsJsonAsync, etc.
                    hasCt = args.Any(IsCancellationTokenArgument);
                    break;
            }
        }

        if (!hasCt)
        {
            string msg;
            if (!string.IsNullOrEmpty(_currentMethodCtParam))
            {
                msg = $"Method '{_currentMember}' receives CancellationToken '{_currentMethodCtParam}' but does not forward it to '{methodName}', risking orphaned background execution.";
            }
            else
            {
                msg = $"Call to '{methodName}' is missing a CancellationToken argument, preventing graceful cancellation and fail-fast under load.";
            }

            AddViolation("missing_cancellation_token", node, msg, "warning");
        }
    }

    private void InspectResilienceCall(InvocationExpressionSyntax node, string methodName)
    {
        switch (methodName)
        {
            case "AddStandardResilienceHandler":
            {
                string? customTimeout = null;
                var arg = node.ArgumentList.Arguments.FirstOrDefault();
                if (arg?.Expression is LambdaExpressionSyntax lambda)
                {
                    var lambdaStr = lambda.ToString();
                    if (lambdaStr.Contains("Timeout"))
                    {
                        customTimeout = "Custom Timeout";
                    }
                }

                _result.ResiliencePipelines.Add(new ResiliencePipeline
                {
                    StrategyType = "StandardResilience",
                    PipelineName = "StandardResilienceHandler",
                    File = _filePath,
                    Line = GetLineNumber(node),
                    ClassName = _currentClass,
                    MethodName = _currentMember,
                    HasJitter = true,
                    BackoffType = "Exponential",
                    TimeoutValue = customTimeout ?? "30s (default)"
                });
                break;
            }

            case "AddResiliencePipeline":
            {
                string? pipelineKey = null;
                var firstArg = node.ArgumentList.Arguments.FirstOrDefault()?.Expression;
                if (firstArg is LiteralExpressionSyntax lit)
                {
                    pipelineKey = lit.Token.ValueText;
                }

                _result.ResiliencePipelines.Add(new ResiliencePipeline
                {
                    StrategyType = "ResiliencePipeline",
                    PipelineName = pipelineKey,
                    File = _filePath,
                    Line = GetLineNumber(node),
                    ClassName = _currentClass,
                    MethodName = _currentMember
                });
                break;
            }

            case "AddRetry":
            {
                InspectPollyV8Retry(node);
                break;
            }

            case "WaitAndRetry":
            case "WaitAndRetryAsync":
            {
                InspectClassicPollyWaitAndRetry(node);
                break;
            }

            case "Retry":
            case "RetryAsync":
            {
                InspectClassicPollyImmediateRetry(node);
                break;
            }

            case "AddCircuitBreaker":
            case "CircuitBreaker":
            case "AdvancedCircuitBreaker":
            {
                var handled = ExtractHandledExceptions(node);
                _result.ResiliencePipelines.Add(new ResiliencePipeline
                {
                    StrategyType = "CircuitBreaker",
                    File = _filePath,
                    Line = GetLineNumber(node),
                    ClassName = _currentClass,
                    MethodName = _currentMember,
                    HandledExceptions = handled
                });
                break;
            }

            case "AddTimeout":
            case "Timeout":
            case "TimeoutAsync":
            {
                string? timeoutVal = null;
                var firstArg = node.ArgumentList.Arguments.FirstOrDefault()?.Expression;
                if (firstArg != null)
                {
                    timeoutVal = ExtractTimeoutValue(firstArg);
                }

                _result.ResiliencePipelines.Add(new ResiliencePipeline
                {
                    StrategyType = "Timeout",
                    File = _filePath,
                    Line = GetLineNumber(node),
                    ClassName = _currentClass,
                    MethodName = _currentMember,
                    TimeoutValue = timeoutVal
                });
                break;
            }

            case "AddPolicyHandler":
            {
                _result.ResiliencePipelines.Add(new ResiliencePipeline
                {
                    StrategyType = "PolicyHandler",
                    PipelineName = node.ArgumentList.Arguments.FirstOrDefault()?.Expression.ToString(),
                    File = _filePath,
                    Line = GetLineNumber(node),
                    ClassName = _currentClass,
                    MethodName = _currentMember
                });
                break;
            }
        }
    }

    private void InspectPollyV8Retry(InvocationExpressionSyntax node)
    {
        bool hasJitter = true; // default in v8 exponential
        string? backoffType = null;
        string? timeoutVal = null;
        var handledExceptions = new List<string>();

        var fullText = node.ToString();

        // Check UseJitter
        if (fullText.Contains("UseJitter"))
        {
            if (System.Text.RegularExpressions.Regex.IsMatch(fullText, @"UseJitter\s*=\s*false", System.Text.RegularExpressions.RegexOptions.IgnoreCase))
            {
                hasJitter = false;
                AddViolation(
                    "unjittered_retry",
                    node,
                    "Retry strategy explicitly disables jitter (UseJitter = false). Retries across instances will synchronize and overwhelm downstreams ('Killed by the Mob' risk).",
                    "error"
                );
            }
            else if (System.Text.RegularExpressions.Regex.IsMatch(fullText, @"UseJitter\s*=\s*true", System.Text.RegularExpressions.RegexOptions.IgnoreCase))
            {
                hasJitter = true;
            }
        }

        // Check BackoffType
        if (fullText.Contains("DelayBackoffType.Constant"))
        {
            backoffType = "Constant";
            if (!fullText.Contains("UseJitter = true") && !fullText.Contains("UseJitter=true"))
            {
                hasJitter = false;
                AddViolation(
                    "unjittered_retry",
                    node,
                    "Retry strategy uses DelayBackoffType.Constant without jitter. Configure DelayBackoffType.Exponential with jitter to prevent retry storms.",
                    "warning"
                );
            }
        }
        else if (fullText.Contains("DelayBackoffType.Linear"))
        {
            backoffType = "Linear";
            if (!fullText.Contains("UseJitter = true") && !fullText.Contains("UseJitter=true"))
            {
                hasJitter = false;
                AddViolation(
                    "unjittered_retry",
                    node,
                    "Retry strategy uses DelayBackoffType.Linear without jitter.",
                    "warning"
                );
            }
        }
        else if (fullText.Contains("DelayBackoffType.Exponential"))
        {
            backoffType = "Exponential";
        }

        // Check ShouldHandle for generic Exception
        if (System.Text.RegularExpressions.Regex.IsMatch(fullText, @"Handle<(\w*\.)?Exception>\(\)") ||
            fullText.Contains("args.Exception is not null"))
        {
            handledExceptions.Add("Exception");
            AddViolation(
                "catch_all_exception_retry",
                node,
                "Retry policy handles generic System.Exception instead of transient/HttpRequestException. Retrying non-transient failures can cause cascading failures and duplicate side-effects.",
                "warning"
            );
        }
        else
        {
            handledExceptions.AddRange(ExtractHandledExceptions(node));
        }

        _result.ResiliencePipelines.Add(new ResiliencePipeline
        {
            StrategyType = "Retry",
            File = _filePath,
            Line = GetLineNumber(node),
            ClassName = _currentClass,
            MethodName = _currentMember,
            HasJitter = hasJitter,
            BackoffType = backoffType ?? "Exponential",
            TimeoutValue = timeoutVal,
            HandledExceptions = handledExceptions
        });
    }

    private void InspectClassicPollyWaitAndRetry(InvocationExpressionSyntax node)
    {
        bool hasJitter = false;
        string? backoffType = null;
        var handledExceptions = ExtractHandledExceptions(node);

        // Check if generic Exception was handled
        var rootExpr = node.Expression;
        var fullChainText = node.ToString();

        if (System.Text.RegularExpressions.Regex.IsMatch(fullChainText, @"Handle<(\w*\.)?Exception>\(\)"))
        {
            handledExceptions.Add("Exception");
            AddViolation(
                "catch_all_exception_retry",
                node,
                "Retry policy handles generic System.Exception instead of transient/HttpRequestException. Retrying non-transient failures can exacerbate outages and cause unintended side-effects.",
                "warning"
            );
        }

        // Check delay provider argument
        var args = node.ArgumentList.Arguments;
        bool usesDecorrelatedJitter = fullChainText.Contains("DecorrelatedJitterBackoffV2") ||
                                      fullChainText.Contains("Jitter") ||
                                      fullChainText.Contains("Random");

        if (usesDecorrelatedJitter)
        {
            hasJitter = true;
            backoffType = "ExponentialWithJitter";
        }
        else
        {
            // If argument is array of TimeSpans or lambda returning TimeSpan without jitter
            bool isFixedDelay = false;
            foreach (var arg in args)
            {
                if (arg.Expression is ArrayCreationExpressionSyntax or ImplicitArrayCreationExpressionSyntax)
                {
                    isFixedDelay = true;
                    backoffType = "ConstantArray";
                    break;
                }
                if (arg.Expression is LambdaExpressionSyntax lambda)
                {
                    var body = lambda.ToString();
                    if (!body.Contains("Jitter") && !body.Contains("Random") && !body.Contains("Decorrelated"))
                    {
                        isFixedDelay = true;
                        backoffType = body.Contains("Math.Pow") ? "Exponential" : "Constant";
                        break;
                    }
                }
            }

            if (isFixedDelay || !hasJitter)
            {
                hasJitter = false;
                AddViolation(
                    "unjittered_retry",
                    node,
                    "Retry policy uses constant or unjittered delay (fixed TimeSpan). Use exponential backoff with decorrelated jitter to prevent 'Killed by the Mob' retry storms.",
                    "warning"
                );
            }
        }

        _result.ResiliencePipelines.Add(new ResiliencePipeline
        {
            StrategyType = "Retry",
            File = _filePath,
            Line = GetLineNumber(node),
            ClassName = _currentClass,
            MethodName = _currentMember,
            HasJitter = hasJitter,
            BackoffType = backoffType ?? "Constant",
            HandledExceptions = handledExceptions
        });
    }

    private void InspectClassicPollyImmediateRetry(InvocationExpressionSyntax node)
    {
        var handledExceptions = ExtractHandledExceptions(node);
        var fullChainText = node.ToString();

        if (System.Text.RegularExpressions.Regex.IsMatch(fullChainText, @"Handle<(\w*\.)?Exception>\(\)"))
        {
            handledExceptions.Add("Exception");
            AddViolation(
                "catch_all_exception_retry",
                node,
                "Retry policy handles generic System.Exception instead of transient/HttpRequestException.",
                "warning"
            );
        }

        // Immediate retry has no delay and no jitter
        AddViolation(
            "unjittered_retry",
            node,
            "Immediate Retry without delay or jitter creates a thundering herd risk under load. Introduce backoff with jitter.",
            "warning"
        );

        _result.ResiliencePipelines.Add(new ResiliencePipeline
        {
            StrategyType = "Retry",
            File = _filePath,
            Line = GetLineNumber(node),
            ClassName = _currentClass,
            MethodName = _currentMember,
            HasJitter = false,
            BackoffType = "Immediate",
            HandledExceptions = handledExceptions
        });
    }

    private static List<InvocationExpressionSyntax> GetInvocationChain(InvocationExpressionSyntax node)
    {
        var chain = new List<InvocationExpressionSyntax>();
        var current = node.Parent;
        while (current is MemberAccessExpressionSyntax parentMember &&
               parentMember.Parent is InvocationExpressionSyntax parentInv)
        {
            chain.Add(parentInv);
            current = parentInv.Parent;
        }
        return chain;
    }

    private static string? GetMethodName(InvocationExpressionSyntax inv)
    {
        if (inv.Expression is MemberAccessExpressionSyntax ma)
        {
            return ma.Name.Identifier.Text;
        }
        if (inv.Expression is IdentifierNameSyntax id)
        {
            return id.Identifier.Text;
        }
        return null;
    }

    private static bool IsCancellationTokenParameter(ParameterSyntax p)
    {
        var typeStr = p.Type?.ToString() ?? "";
        return typeStr.Contains("CancellationToken") || p.Identifier.Text is "cancellationToken" or "ct";
    }

    private static bool IsCancellationTokenArgument(ArgumentSyntax arg)
    {
        var text = arg.Expression.ToString();
        if (text is "cancellationToken" or "ct" or "token" or "cancelToken")
            return true;

        if (text.EndsWith(".Token", StringComparison.OrdinalIgnoreCase) ||
            text.EndsWith(".CancellationToken", StringComparison.OrdinalIgnoreCase) ||
            text.EndsWith(".RequestAborted", StringComparison.OrdinalIgnoreCase) ||
            text.Equals("CancellationToken.None", StringComparison.OrdinalIgnoreCase))
            return true;

        if (text.Contains("CancellationToken"))
            return true;

        return false;
    }

    private static bool IsBooleanLiteral(ExpressionSyntax expr)
    {
        return expr.Kind() is SyntaxKind.TrueLiteralExpression or SyntaxKind.FalseLiteralExpression;
    }

    private static string? GetAssignedVariableName(SyntaxNode node)
    {
        if (node.Parent is EqualsValueClauseSyntax eq && eq.Parent is VariableDeclaratorSyntax dec)
        {
            return dec.Identifier.Text;
        }
        if (node.Parent is AssignmentExpressionSyntax assign && assign.Left is IdentifierNameSyntax id)
        {
            return id.Identifier.Text;
        }
        return null;
    }

    public static string? ExtractTimeoutValue(ExpressionSyntax expr)
    {
        if (expr == null) return null;

        // TimeSpan.FromSeconds(5) or TimeSpan.FromMilliseconds(500)
        if (expr is InvocationExpressionSyntax inv)
        {
            var methodText = inv.Expression.ToString();
            var arg = inv.ArgumentList.Arguments.FirstOrDefault()?.Expression;
            var argText = arg?.ToString() ?? "";

            if (methodText.EndsWith("FromSeconds", StringComparison.Ordinal))
                return $"{argText}s";
            if (methodText.EndsWith("FromMilliseconds", StringComparison.Ordinal))
                return $"{argText}ms";
            if (methodText.EndsWith("FromMinutes", StringComparison.Ordinal))
                return $"{argText}m";
            if (methodText.EndsWith("FromHours", StringComparison.Ordinal))
                return $"{argText}h";
        }

        // new TimeoutStrategyOptions { Timeout = TimeSpan.FromSeconds(5) }
        if (expr is ObjectCreationExpressionSyntax objInit && objInit.Initializer != null)
        {
            foreach (var initExpr in objInit.Initializer.Expressions)
            {
                if (initExpr is AssignmentExpressionSyntax assign && assign.Left.ToString() == "Timeout")
                {
                    return ExtractTimeoutValue(assign.Right);
                }
            }
        }

        // new TimeSpan(h, m, s)
        if (expr is ObjectCreationExpressionSyntax objCreation)
        {
            var args = objCreation.ArgumentList?.Arguments;
            if (args != null && args.Value.Count == 3)
            {
                return $"{args.Value[0]}h {args.Value[1]}m {args.Value[2]}s";
            }
        }

        // Literal number
        if (expr is LiteralExpressionSyntax lit && lit.Token.Value is int or double)
        {
            return $"{lit.Token.ValueText}s";
        }

        return expr.ToString();
    }

    private static List<string> ExtractHandledExceptions(SyntaxNode node)
    {
        var list = new List<string>();
        foreach (var inv in node.DescendantNodesAndSelf().OfType<InvocationExpressionSyntax>())
        {
            if (inv.Expression is MemberAccessExpressionSyntax memberAccess &&
                memberAccess.Name is GenericNameSyntax genName &&
                genName.Identifier.Text == "Handle")
            {
                foreach (var typeArg in genName.TypeArgumentList.Arguments)
                {
                    list.Add(typeArg.ToString());
                }
            }
            else if (inv.Expression.ToString().Contains("HandleTransientHttpError"))
            {
                list.Add("TransientHttpError");
            }
        }
        return list;
    }

    private static readonly HashSet<string> RemoteIoMethodNames = new(StringComparer.OrdinalIgnoreCase)
    {
        // HTTP
        "GetAsync", "PostAsync", "PutAsync", "DeleteAsync", "SendAsync",
        "GetStringAsync", "GetByteArrayAsync", "GetStreamAsync",
        "PostAsJsonAsync", "PutAsJsonAsync", "PatchAsync", "ExecuteAsync",
        // Relational DB & Dapper
        "ExecuteNonQuery", "ExecuteNonQueryAsync", "ExecuteReader", "ExecuteReaderAsync",
        "ExecuteScalar", "ExecuteScalarAsync", "Query", "QueryAsync",
        "QueryFirstOrDefault", "QueryFirstOrDefaultAsync", "QuerySingle", "QuerySingleAsync",
        "QueryMultiple", "QueryMultipleAsync", "Execute",
        // ORM
        "SaveChangesAsync", "SaveChanges",
        // Cache (Redis)
        "StringGet", "StringGetAsync", "StringSet", "StringSetAsync", "KeyDelete", "KeyDeleteAsync",
        // Message Brokers
        "Publish", "PublishAsync", "Send", "SendAsync", "BasicPublish", "ProduceAsync"
    };

    private void InspectForSuspiciousCustomResilience(MethodDeclarationSyntax method)
    {
        if (method.Body == null && method.ExpressionBody == null)
            return;

        // 1. Check for loops containing try-catch blocks
        var loops = method.DescendantNodes().Where(n => n is ForStatementSyntax or WhileStatementSyntax or DoStatementSyntax or ForEachStatementSyntax);
        foreach (var loop in loops)
        {
            var tryBlocks = loop.DescendantNodes().OfType<TryStatementSyntax>().ToList();
            if (!tryBlocks.Any())
                continue;

            // Vertex A: Does the try block (or loop) call a Remote I/O Anchor?
            string? anchorCall = FindRemoteIoAnchor(loop, method);
            if (anchorCall == null)
                continue;

            // Vertex C: Does the loop or its catch blocks contain a Delay or Retry Counter signal?
            var (hasDelayOrCounter, detail) = HasDelayOrRetryCounterSignal(loop, tryBlocks);
            if (!hasDelayOrCounter)
                continue;

            // All 3 Vertices met!
            AddViolation(
                "suspicious_custom_resilience",
                loop,
                $"Suspicious custom retry loop wrapping remote I/O anchor '{anchorCall}' ({detail}). Hand-rolled retry loops often cause threadpool starvation (blocking sleep), 'Killed by the Mob' retry storms (missing jitter), or duplicate side-effects on non-idempotent endpoints. Recommended action: Audit with AI and migrate to standard Polly v8 pipelines.",
                "warning"
            );
        }

        // 2. Check for Task.WhenAny naive timeout wrappers without linked cancellation
        var whenAnyInvocations = method.DescendantNodes().OfType<InvocationExpressionSyntax>()
            .Where(inv => GetMethodName(inv) == "WhenAny");

        foreach (var whenAny in whenAnyInvocations)
        {
            var args = whenAny.ArgumentList.Arguments;
            if (args.Count < 2) continue;

            bool hasDelayArg = args.Any(a => a.Expression.ToString().Contains("Task.Delay"));
            string? anchorCall = null;
            foreach (var arg in args)
            {
                var match = FindRemoteIoAnchor(arg.Expression, method);
                if (match != null)
                {
                    anchorCall = match;
                    break;
                }
            }

            if (hasDelayArg && anchorCall != null)
            {
                AddViolation(
                    "suspicious_custom_resilience",
                    whenAny,
                    $"Suspicious naive timeout using Task.WhenAny wrapping remote I/O anchor '{anchorCall}' with Task.Delay. In .NET, if the losing task is not explicitly cancelled via CancellationTokenSource, it continues running invisibly in the background, leaking socket handles and thread resources. Recommended action: Audit with AI and use CancellationTokenSource(TimeSpan) or Polly timeout.",
                    "warning"
                );
            }
        }
    }

    private string? FindRemoteIoAnchor(SyntaxNode node, MethodDeclarationSyntax? enclosingMethod = null)
    {
        foreach (var inv in node.DescendantNodesAndSelf().OfType<InvocationExpressionSyntax>())
        {
            var name = GetMethodName(inv);
            if (name != null && RemoteIoMethodNames.Contains(name))
            {
                return name;
            }

            if (inv.Expression is MemberAccessExpressionSyntax ma)
            {
                var maText = ma.ToString();
                if (RemoteIoMethodNames.Any(m => maText.EndsWith("." + m, StringComparison.OrdinalIgnoreCase)))
                {
                    return ma.Name.Identifier.Text;
                }
            }
        }

        if (enclosingMethod != null)
        {
            foreach (var id in node.DescendantNodesAndSelf().OfType<IdentifierNameSyntax>())
            {
                var varName = id.Identifier.Text;
                var declarators = enclosingMethod.DescendantNodes().OfType<VariableDeclaratorSyntax>()
                    .Where(d => d.Identifier.Text == varName && d.Initializer != null);
                foreach (var dec in declarators)
                {
                    var match = FindRemoteIoAnchor(dec.Initializer!.Value);
                    if (match != null)
                        return match;
                }
            }
        }

        return null;
    }

    private static (bool, string) HasDelayOrRetryCounterSignal(SyntaxNode loop, List<TryStatementSyntax> tryBlocks)
    {
        if (loop.ToString().Contains("Thread.Sleep"))
        {
            return (true, "contains blocking Thread.Sleep");
        }

        if (loop.ToString().Contains("Task.Delay"))
        {
            return (true, "contains Task.Delay");
        }

        var loopText = loop.ToString();
        var counterMatches = System.Text.RegularExpressions.Regex.IsMatch(
            loopText,
            @"\b(attempt|retries|retry|tryCount|tries|maxRetries)\b",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase
        );

        if (counterMatches)
        {
            if (System.Text.RegularExpressions.Regex.IsMatch(loopText, @"\b(attempt|retries|retry|tryCount|tries)\s*(\+\+|\-\-|\+=|\-=|<|>|<=|>=)"))
            {
                return (true, "contains retry attempt counter and catch-retry loop");
            }
        }

        foreach (var tryBlock in tryBlocks)
        {
            foreach (var catchClause in tryBlock.Catches)
            {
                var catchText = catchClause.ToString();
                if (catchText.Contains("Thread.Sleep"))
                    return (true, "contains blocking Thread.Sleep in catch block");
                if (catchText.Contains("Task.Delay"))
                    return (true, "contains Task.Delay in catch block");
                if (System.Text.RegularExpressions.Regex.IsMatch(catchText, @"\b(attempt|retries|retry|tryCount)\b"))
                    return (true, "manages retry attempt state in catch block");
            }
        }

        return (false, "");
    }

    private bool IsTelemetryOrLogging(SyntaxNode node)
    {
        var path = _filePath.Replace('\\', '/');
        if (path.Contains("/Logging/", StringComparison.OrdinalIgnoreCase) ||
            path.Contains("/Telemetry/", StringComparison.OrdinalIgnoreCase) ||
            path.Contains("/Metrics/", StringComparison.OrdinalIgnoreCase) ||
            path.Contains("/Diagnostics/", StringComparison.OrdinalIgnoreCase) ||
            path.Contains("/Serilog/", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        var ns = node.Ancestors().OfType<BaseNamespaceDeclarationSyntax>().FirstOrDefault()?.Name.ToString() ?? "";
        if (ns.Contains("Logging", StringComparison.OrdinalIgnoreCase) ||
            ns.Contains("Telemetry", StringComparison.OrdinalIgnoreCase) ||
            ns.Contains("Metrics", StringComparison.OrdinalIgnoreCase) ||
            ns.Contains("Diagnostics", StringComparison.OrdinalIgnoreCase) ||
            ns.Contains("Serilog", StringComparison.OrdinalIgnoreCase) ||
            ns.Contains("Audit", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        if (_currentClass.EndsWith("Logger", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.EndsWith("Logging", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.EndsWith("LogSink", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.StartsWith("Logging", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.StartsWith("Logger", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.Contains("Telemetry", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.Contains("Metric", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.EndsWith("TelemetrySink", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.EndsWith("MetricsSink", StringComparison.OrdinalIgnoreCase) ||
            _currentClass.EndsWith("BatchSink", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        return false;
    }

    private static bool IsGuardedBySemaphoreOrLimiter(SyntaxNode node)
    {
        var enclosingClass = node.Ancestors().OfType<TypeDeclarationSyntax>().FirstOrDefault();
        if (enclosingClass == null) return false;

        var classText = enclosingClass.ToString();
        return classText.Contains("SemaphoreSlim") ||
               classText.Contains("RateLimiter") ||
               classText.Contains("PartitionedRateLimiter");
    }

    private bool DoesChannelEscape(SyntaxNode node)
    {
        if (node.Ancestors().OfType<FieldDeclarationSyntax>().Any())
            return true;
        if (node.Ancestors().OfType<PropertyDeclarationSyntax>().Any())
            return true;
        if (node.Ancestors().OfType<ConstructorDeclarationSyntax>().Any())
            return true;

        // Check if inside DI registration call
        var enclosingInvocation = node.Ancestors().OfType<InvocationExpressionSyntax>()
            .FirstOrDefault(inv => inv != node);
        if (enclosingInvocation != null)
        {
            var invText = enclosingInvocation.Expression.ToString();
            if (invText.Contains("AddSingleton") || invText.Contains("AddTransient") || invText.Contains("AddScoped"))
                return true;
        }

        // Check if in Top-Level Statements / Program / Startup
        if (_currentClass is "Program" or "Startup")
            return true;

        // Check if in a method: if assigned to a local variable, does it escape?
        var method = node.Ancestors().OfType<MethodDeclarationSyntax>().FirstOrDefault();
        var enclosingClass = node.Ancestors().OfType<TypeDeclarationSyntax>().FirstOrDefault();
        if (method != null)
        {
            var declarator = node.Ancestors().OfType<VariableDeclaratorSyntax>().FirstOrDefault();
            if (declarator != null)
            {
                var varName = declarator.Identifier.Text;
                // Does this variable escape by return?
                foreach (var ret in method.DescendantNodes().OfType<ReturnStatementSyntax>())
                {
                    if (ret.Expression != null && ret.Expression.ToString().Contains(varName))
                        return true;
                }
                // Does this variable get assigned to a field or property?
                foreach (var assign in method.DescendantNodes().OfType<AssignmentExpressionSyntax>())
                {
                    if (assign.Right.ToString().Contains(varName))
                    {
                        var leftStr = assign.Left.ToString();
                        if (leftStr.StartsWith("_") || leftStr.StartsWith("this.") ||
                            (enclosingClass != null && (
                                enclosingClass.Members.OfType<FieldDeclarationSyntax>().Any(f => f.Declaration.Variables.Any(v => v.Identifier.Text == leftStr)) ||
                                enclosingClass.Members.OfType<PropertyDeclarationSyntax>().Any(p => p.Identifier.Text == leftStr)
                            )))
                        {
                            return true;
                        }
                    }
                }
                // Does this variable escape via registration method?
                foreach (var inv in method.DescendantNodes().OfType<InvocationExpressionSyntax>())
                {
                    if (inv.ArgumentList.Arguments.Any(a => a.ToString().Contains(varName)))
                    {
                        var invText = inv.Expression.ToString();
                        if (invText.Contains("Register") || invText.Contains("Subscribe") || invText.Contains("Add") || invText.Contains("Start"))
                            return true;
                    }
                }
                // Method-local fan-out / fan-in: does not escape
                return false;
            }
            // Direct return: return Channel.CreateUnbounded<T>();
            if (node.Ancestors().OfType<ReturnStatementSyntax>().Any())
                return true;
        }

        return true;
    }

    private void InspectChannelCreateUnbounded(InvocationExpressionSyntax node)
    {
        var exprStr = node.Expression.ToString();
        if (!exprStr.Contains("Channel") && !exprStr.Contains("CreateUnbounded"))
            return;

        if (IsTelemetryOrLogging(node))
            return;

        if (!DoesChannelEscape(node))
            return;

        bool isGuarded = IsGuardedBySemaphoreOrLimiter(node);
        string severity = isGuarded ? "warning" : "error";
        string message = isGuarded
            ? "Unbounded channel at architectural boundary is constrained by an upstream semaphore or limiter, but lacks native backpressure flow control. Prefer Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait."
            : "Unbounded channel at architectural boundary creates a buffer without backpressure. Under downstream latency or stalls, memory grows monotonically until process termination by the Linux cgroup OOM killer. Migrate to Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait.";

        AddViolation("unbounded_boundary_channel", node, message, severity);
    }

    private void InspectChannelCreateBounded(InvocationExpressionSyntax node)
    {
        var exprStr = node.Expression.ToString();
        if (!exprStr.Contains("Channel") && !exprStr.Contains("CreateBounded"))
            return;

        if (IsTelemetryOrLogging(node)) return;

        if (node.ArgumentList.Arguments.Count > 0)
        {
            var firstArg = node.ArgumentList.Arguments[0].Expression;
            var firstArgText = firstArg.ToString();

            if (firstArgText.Contains("int.MaxValue"))
            {
                AddViolation(
                    "unbounded_boundary_channel",
                    node,
                    "Bounded channel configured with int.MaxValue is effectively unbounded, providing no backpressure. Use a sized capacity budgeted against container memory limits.",
                    "error"
                );
            }
            else if (firstArg is LiteralExpressionSyntax lit && lit.Token.Value is int capacity && capacity > 50000)
            {
                AddViolation(
                    "unbounded_boundary_channel",
                    node,
                    $"Bounded channel capacity ({capacity:N0}) exceeds safe memory envelope for typical container limits (>50,000 items). Under latency, retained memory may cause excessive Gen 2 GC pressure or OOM. Tune capacity to expected processing rate and memory budget.",
                    "warning"
                );
            }
        }
    }

    private void InspectFieldForUnboundedQueue(FieldDeclarationSyntax node)
    {
        if (IsTelemetryOrLogging(node)) return;

        var typeStr = node.Declaration.Type.ToString();
        var varName = node.Declaration.Variables.FirstOrDefault()?.Identifier.Text ?? "";
        if (varName.Contains("Pool", StringComparison.OrdinalIgnoreCase) ||
            varName.Contains("FreeList", StringComparison.OrdinalIgnoreCase))
        {
            return; // Internal object / buffer pools are not boundary ingress queues
        }

        if (typeStr.Contains("ConcurrentQueue<"))
        {
            bool isGuarded = IsGuardedBySemaphoreOrLimiter(node);
            string severity = isGuarded ? "warning" : "error";
            string msg = isGuarded
                ? "ConcurrentQueue<T> used as shared boundary state is guarded by a semaphore/limiter, but lacks native backpressure flow control. Prefer Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait."
                : "ConcurrentQueue<T> used as shared boundary state provides no backpressure API. Under worker lag or downstream stalls, queue depth grows unconstrained, risking Gen 2 GC compaction spikes and Linux OOM-kill. Migrate to Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait.";

            AddViolation("unbounded_boundary_channel", node, msg, severity);
        }
        else if (typeStr.Contains("BlockingCollection<"))
        {
            bool isGuarded = IsGuardedBySemaphoreOrLimiter(node);
            string severity = isGuarded ? "warning" : "error";
            var initExpr = node.Declaration.Variables.FirstOrDefault()?.Initializer?.Value;
            bool isUnbounded = false;
            int? explicitCap = null;
            bool isMaxVal = false;

            if (initExpr is ImplicitObjectCreationExpressionSyntax implicitNew)
            {
                if (implicitNew.ArgumentList == null || implicitNew.ArgumentList.Arguments.Count == 0 ||
                    implicitNew.ArgumentList.Arguments[0].ToString().Contains("ConcurrentQueue"))
                {
                    isUnbounded = true;
                }
                else if (implicitNew.ArgumentList.Arguments.Count > 0)
                {
                    var arg = implicitNew.ArgumentList.Arguments[0].Expression;
                    if (arg.ToString().Contains("int.MaxValue")) isMaxVal = true;
                    else if (arg is LiteralExpressionSyntax lit && lit.Token.Value is int cap) explicitCap = cap;
                }
            }
            else if (initExpr is ObjectCreationExpressionSyntax explicitNew)
            {
                if (explicitNew.ArgumentList == null || explicitNew.ArgumentList.Arguments.Count == 0 ||
                    explicitNew.ArgumentList.Arguments[0].ToString().Contains("ConcurrentQueue"))
                {
                    isUnbounded = true;
                }
                else if (explicitNew.ArgumentList.Arguments.Count > 0)
                {
                    var arg = explicitNew.ArgumentList.Arguments[0].Expression;
                    if (arg.ToString().Contains("int.MaxValue")) isMaxVal = true;
                    else if (arg is LiteralExpressionSyntax lit && lit.Token.Value is int cap) explicitCap = cap;
                }
            }
            else if (initExpr != null)
            {
                var initText = initExpr.ToString();
                isUnbounded = initText.StartsWith("new()") || (initText.Contains("new BlockingCollection") && (!initText.Contains("(") || initText.Contains("()") || initText.Contains("ConcurrentQueue")));
            }

            if (isUnbounded || isMaxVal)
            {
                string msg = isGuarded
                    ? "BlockingCollection<T> configured with unbounded capacity (int.MaxValue) is guarded by semaphore/limiter, but lacks native backpressure flow control. Migrate to a bounded capacity or Channel.CreateBounded<T>(capacity)."
                    : "BlockingCollection<T> instantiated with default unbounded capacity (int.MaxValue). Under worker lag or downstream stalls, queue depth grows unconstrained, risking Gen 2 GC compaction spikes and Linux OOM-kill. Migrate to a bounded capacity (e.g. new BlockingCollection<T>(capacity)) or Channel.CreateBounded<T>(capacity).";
                AddViolation("unbounded_boundary_channel", node, msg, severity);
            }
            else if (explicitCap.HasValue && explicitCap.Value > 50000)
            {
                AddViolation(
                    "unbounded_boundary_channel",
                    node,
                    $"BlockingCollection<T> capacity ({explicitCap.Value:N0}) exceeds safe memory envelope for typical container limits (>50,000 items). Under latency, retained memory may cause excessive Gen 2 GC pressure or OOM. Tune capacity to expected processing rate and memory budget.",
                    "warning"
                );
            }
        }
    }

    private void InspectPropertyForUnboundedQueue(PropertyDeclarationSyntax node)
    {
        if (IsTelemetryOrLogging(node)) return;

        var typeStr = node.Type.ToString();
        var propName = node.Identifier.Text;
        if (propName.Contains("Pool", StringComparison.OrdinalIgnoreCase) ||
            propName.Contains("FreeList", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        if (typeStr.Contains("ConcurrentQueue<"))
        {
            bool isGuarded = IsGuardedBySemaphoreOrLimiter(node);
            string severity = isGuarded ? "warning" : "error";
            string msg = isGuarded
                ? "ConcurrentQueue<T> used as shared boundary state is guarded by a semaphore/limiter, but lacks native backpressure flow control. Prefer Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait."
                : "ConcurrentQueue<T> used as shared boundary state provides no backpressure API. Under worker lag or downstream stalls, queue depth grows unconstrained, risking Gen 2 GC compaction spikes and Linux OOM-kill. Migrate to Channel.CreateBounded<T>(capacity) with BoundedChannelFullMode.Wait.";

            AddViolation("unbounded_boundary_channel", node, msg, severity);
        }
        else if (typeStr.Contains("BlockingCollection<"))
        {
            bool isGuarded = IsGuardedBySemaphoreOrLimiter(node);
            string severity = isGuarded ? "warning" : "error";
            var initExpr = node.Initializer?.Value;
            bool isUnbounded = false;
            int? explicitCap = null;
            bool isMaxVal = false;

            if (initExpr is ImplicitObjectCreationExpressionSyntax implicitNew)
            {
                if (implicitNew.ArgumentList == null || implicitNew.ArgumentList.Arguments.Count == 0 ||
                    implicitNew.ArgumentList.Arguments[0].ToString().Contains("ConcurrentQueue"))
                {
                    isUnbounded = true;
                }
                else if (implicitNew.ArgumentList.Arguments.Count > 0)
                {
                    var arg = implicitNew.ArgumentList.Arguments[0].Expression;
                    if (arg.ToString().Contains("int.MaxValue")) isMaxVal = true;
                    else if (arg is LiteralExpressionSyntax lit && lit.Token.Value is int cap) explicitCap = cap;
                }
            }
            else if (initExpr is ObjectCreationExpressionSyntax explicitNew)
            {
                if (explicitNew.ArgumentList == null || explicitNew.ArgumentList.Arguments.Count == 0 ||
                    explicitNew.ArgumentList.Arguments[0].ToString().Contains("ConcurrentQueue"))
                {
                    isUnbounded = true;
                }
                else if (explicitNew.ArgumentList.Arguments.Count > 0)
                {
                    var arg = explicitNew.ArgumentList.Arguments[0].Expression;
                    if (arg.ToString().Contains("int.MaxValue")) isMaxVal = true;
                    else if (arg is LiteralExpressionSyntax lit && lit.Token.Value is int cap) explicitCap = cap;
                }
            }
            else if (initExpr != null)
            {
                var initText = initExpr.ToString();
                isUnbounded = initText.StartsWith("new()") || (initText.Contains("new BlockingCollection") && (!initText.Contains("(") || initText.Contains("()") || initText.Contains("ConcurrentQueue")));
            }

            if (isUnbounded || isMaxVal)
            {
                string msg = isGuarded
                    ? "BlockingCollection<T> configured with unbounded capacity (int.MaxValue) is guarded by semaphore/limiter, but lacks native backpressure flow control. Migrate to a bounded capacity or Channel.CreateBounded<T>(capacity)."
                    : "BlockingCollection<T> instantiated with default unbounded capacity (int.MaxValue). Under worker lag or downstream stalls, queue depth grows unconstrained, risking Gen 2 GC compaction spikes and Linux OOM-kill. Migrate to a bounded capacity (e.g. new BlockingCollection<T>(capacity)) or Channel.CreateBounded<T>(capacity).";
                AddViolation("unbounded_boundary_channel", node, msg, severity);
            }
            else if (explicitCap.HasValue && explicitCap.Value > 50000)
            {
                AddViolation(
                    "unbounded_boundary_channel",
                    node,
                    $"BlockingCollection<T> capacity ({explicitCap.Value:N0}) exceeds safe memory envelope for typical container limits (>50,000 items). Under latency, retained memory may cause excessive Gen 2 GC pressure or OOM. Tune capacity to expected processing rate and memory budget.",
                    "warning"
                );
            }
        }
    }

    private void InspectObjectCreationForUnboundedQueue(ObjectCreationExpressionSyntax node)
    {
        var typeStr = node.Type.ToString();
        if (typeStr.Contains("BlockingCollection<"))
        {
            if (!IsTelemetryOrLogging(node) && DoesChannelEscape(node))
            {
                bool isGuarded = IsGuardedBySemaphoreOrLimiter(node);
                string severity = isGuarded ? "warning" : "error";

                if (node.ArgumentList == null || node.ArgumentList.Arguments.Count == 0 ||
                    (node.ArgumentList.Arguments.Count == 1 && node.ArgumentList.Arguments[0].ToString().Contains("ConcurrentQueue")))
                {
                    string msg = isGuarded
                        ? "BlockingCollection<T> instantiated with default unbounded capacity (int.MaxValue) is guarded by semaphore/limiter, but lacks native backpressure flow control. Migrate to a bounded capacity or Channel.CreateBounded<T>(capacity)."
                        : "BlockingCollection<T> instantiated with default unbounded capacity (int.MaxValue). Under worker lag or downstream stalls, queue depth grows unconstrained, risking Gen 2 GC compaction spikes and Linux OOM-kill. Migrate to a bounded capacity (e.g. new BlockingCollection<T>(capacity)) or Channel.CreateBounded<T>(capacity).";
                    AddViolation("unbounded_boundary_channel", node, msg, severity);
                }
                else if (node.ArgumentList.Arguments.Count > 0)
                {
                    var firstArg = node.ArgumentList.Arguments[0].Expression;
                    var firstArgText = firstArg.ToString();
                    if (firstArgText.Contains("int.MaxValue"))
                    {
                        AddViolation(
                            "unbounded_boundary_channel",
                            node,
                            "BlockingCollection<T> configured with int.MaxValue is effectively unbounded, providing no backpressure. Use a sized capacity budgeted against container memory limits.",
                            severity
                        );
                    }
                    else if (firstArg is LiteralExpressionSyntax lit && lit.Token.Value is int capacity && capacity > 50000)
                    {
                        AddViolation(
                            "unbounded_boundary_channel",
                            node,
                            $"BlockingCollection<T> capacity ({capacity:N0}) exceeds safe memory envelope for typical container limits (>50,000 items). Under latency, retained memory may cause excessive Gen 2 GC pressure or OOM. Tune capacity to expected processing rate and memory budget.",
                            "warning"
                        );
                    }
                }
            }
        }
        else if (typeStr.Contains("BoundedChannelOptions"))
        {
            if (!IsTelemetryOrLogging(node) && node.ArgumentList != null && node.ArgumentList.Arguments.Count > 0)
            {
                var firstArg = node.ArgumentList.Arguments[0].Expression;
                var firstArgText = firstArg.ToString();
                if (firstArgText.Contains("int.MaxValue"))
                {
                    AddViolation(
                        "unbounded_boundary_channel",
                        node,
                        "BoundedChannelOptions configured with int.MaxValue is effectively unbounded, providing no backpressure. Use a sized capacity budgeted against container memory limits.",
                        "error"
                    );
                }
                else if (firstArg is LiteralExpressionSyntax lit && lit.Token.Value is int capacity && capacity > 50000)
                {
                    AddViolation(
                        "unbounded_boundary_channel",
                        node,
                        $"BoundedChannelOptions capacity ({capacity:N0}) exceeds safe memory envelope for typical container limits (>50,000 items). Under latency, retained memory may cause excessive Gen 2 GC pressure or OOM. Tune capacity to expected processing rate and memory budget.",
                        "warning"
                    );
                }
            }
        }
    }
}
