using System;
using System.Data;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Polly;
using Polly.Retry;
using Polly.Timeout;
using StackExchange.Redis;
using MassTransit;

namespace SampleApp.Infrastructure;

public class TestDbContext : DbContext
{
    public TestDbContext(DbContextOptions<TestDbContext> options) : base(options) { }
}

public class OrderServiceClient
{
    private readonly HttpClient _httpClient;
    private readonly TestDbContext _dbContext;
    private readonly IDbConnection _dbConnection;
    private readonly IConnectionMultiplexer _redis;
    private readonly IBus _bus;

    public OrderServiceClient(
        HttpClient httpClient,
        TestDbContext dbContext,
        IDbConnection dbConnection,
        IConnectionMultiplexer redis,
        IBus bus)
    {
        _httpClient = httpClient;
        _dbContext = dbContext;
        _dbConnection = dbConnection;
        _redis = redis;
        _bus = bus;
    }

    // VIOLATION: missing_cancellation_token on GetAsync and SaveChangesAsync
    public async Task ProcessOrderViolationsAsync(string orderId, CancellationToken cancellationToken)
    {
        // Missing CancellationToken: not forwarding cancellationToken
        var response = await _httpClient.GetAsync($"https://api.example.com/orders/{orderId}");

        // Missing CancellationToken: no args
        await _dbContext.SaveChangesAsync();

        // Local HttpClient instantiation with default timeout
        var tempClient = new HttpClient();
    }

    // CLEAN: passes CancellationToken and sets explicit timeout
    public async Task ProcessOrderCleanAsync(string orderId, CancellationToken cancellationToken)
    {
        var response = await _httpClient.GetAsync($"https://api.example.com/orders/{orderId}", cancellationToken);
        await _dbContext.SaveChangesAsync(cancellationToken);

        var tempClient = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
    }
}

public static class ResilienceConfiguration
{
    public static void ConfigureServices(IServiceCollection services)
    {
        // VIOLATION: default_timeout (no timeout or resilience configured)
        services.AddHttpClient("UnconfiguredClient");

        // CLEAN: explicit timeout
        services.AddHttpClient("TimeoutConfiguredClient")
            .ConfigureHttpClient(c => c.Timeout = TimeSpan.FromSeconds(10));

        // CLEAN: standard resilience handler attached
        services.AddHttpClient("ResilientClient")
            .AddStandardResilienceHandler();

        // VIOLATION: catch_all_exception_retry & unjittered_retry (classic Polly)
        var badPolicy = Policy
            .Handle<Exception>()
            .WaitAndRetry(3, _ => TimeSpan.FromSeconds(2));

        // CLEAN: transient error with jittered backoff
        var cleanPolicy = Policy
            .Handle<HttpRequestException>()
            .WaitAndRetryAsync(Backoff.DecorrelatedJitterBackoffV2(TimeSpan.FromSeconds(1), 3));

        // VIOLATION: Polly v8 retry with UseJitter = false
        services.AddResiliencePipeline("v8-bad-pipeline", builder =>
        {
            builder.AddRetry(new RetryStrategyOptions
            {
                BackoffType = DelayBackoffType.Constant,
                UseJitter = false
            });
            builder.AddTimeout(TimeSpan.FromSeconds(5));
        });

        // CLEAN: Polly v8 retry with Exponential backoff & jitter
        services.AddResiliencePipeline("v8-clean-pipeline", builder =>
        {
            builder.AddRetry(new RetryStrategyOptions
            {
                BackoffType = DelayBackoffType.Exponential,
                UseJitter = true
            });
            builder.AddTimeout(TimeSpan.FromSeconds(10));
        });
    }
}
