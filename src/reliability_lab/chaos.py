from __future__ import annotations

import copy
import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = entry["ts"]
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _execute_single_request(gateway: ReliabilityGateway, queries: list[str]) -> dict:
    """Execute a single request and return result dict for aggregation."""
    prompt = random.choice(queries)
    result = gateway.complete(prompt)
    
    return {
        "cost": result.estimated_cost,
        "cache_hit": result.cache_hit,
        "route": result.route,
        "latency_ms": result.latency_ms,
    }


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario.
    
    Supports both sequential and concurrent execution:
    - Sequential: requests run one-by-one (default, concurrency=1)
    - Concurrent: requests run in thread pool (stretch goal, concurrency>1)
    """
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = config.load_test.requests
    concurrency = config.load_test.concurrency
    
    # Execute requests sequentially or concurrently
    if concurrency and concurrency > 1:
        # Concurrent execution with ThreadPoolExecutor
        results = []
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(_execute_single_request, gateway, queries)
                for _ in range(request_count)
            ]
            for future in as_completed(futures):
                results.append(future.result())
    else:
        # Sequential execution (default)
        results = [
            _execute_single_request(gateway, queries)
            for _ in range(request_count)
        ]
    
    # Aggregate results
    for result in results:
        metrics.total_requests += 1
        metrics.estimated_cost += result["cost"]
        if result["cache_hit"]:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        # Check if route starts with "fallback" (new format: "fallback:provider_name")
        if result["route"].startswith("fallback"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result["route"] == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result["latency_ms"]:
            metrics.latencies_ms.append(result["latency_ms"])

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.
    
    Includes cache vs no-cache comparison scenarios for observing impact of caching
    on latency, cost, and error rates.
    """
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        # Define pass/fail criteria per scenario:
        # Account for cache hits: fallback_success_rate should be measured only on non-cache requests
        non_cache_requests = result.total_requests - result.cache_hits
        
        passed = True
        if scenario.name == "primary_timeout_100":
            # When primary is completely down, fallback should handle all non-cache requests
            # Pass if fallback served majority (>80%) of non-cache traffic
            if non_cache_requests > 0:
                fallback_rate = result.fallback_successes / non_cache_requests
                passed = fallback_rate > 0.8
            else:
                # All requests were cache hits — still a success
                passed = True
        elif scenario.name == "primary_flaky_50":
            # When primary is flaky (50%), we should handle ~70%+ of all requests
            success_rate = (
                result.successful_requests / result.total_requests 
                if result.total_requests > 0 else 0
            )
            passed = success_rate >= 0.7
        elif scenario.name == "cache_stale_candidate":
            # Test improved similarity prevents false hits
            # Pass if: high cache hit rate (>20%) AND all requests succeeded (no stale data)
            cache_hit_rate = result.cache_hits / result.total_requests if result.total_requests > 0 else 0
            success_rate = result.successful_requests / result.total_requests if result.total_requests > 0 else 0
            passed = cache_hit_rate > 0.2 and (success_rate + result.fallback_successes / max(result.total_requests, 1)) >= 0.95
        else:
            # Default: pass if we have successful requests or cache hits
            passed = result.successful_requests > 0 or result.cache_hits > 0
        
        combined.scenarios[scenario.name] = "pass" if passed else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined
