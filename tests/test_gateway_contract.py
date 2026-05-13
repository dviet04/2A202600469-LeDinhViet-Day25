from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    # Route format: "primary:provider_name", "fallback:provider_name", "static_fallback", or "cache_hit:score"
    assert result.route.startswith(("primary:", "fallback:", "cache_hit:", "static_fallback"))


def test_circuit_breaker_opens_and_fallback_serves() -> None:
    """Verify circuit breaker opens after N failures, then backup provider serves requests."""
    # Create two providers: primary (will fail) and backup (healthy)
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=10, cost_per_1k_tokens=0.01)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=20, cost_per_1k_tokens=0.005)
    
    # Create circuit breakers with low failure threshold (2 failures = open)
    primary_breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=5)
    backup_breaker = CircuitBreaker("backup", failure_threshold=5, reset_timeout_seconds=5)
    
    gateway = ReliabilityGateway(
        providers=[primary, backup],
        breakers={"primary": primary_breaker, "backup": backup_breaker},
    )
    
    # Force primary to fail 2 times — circuit should OPEN
    result1 = gateway.complete("request 1")
    assert primary_breaker.state == CircuitState.CLOSED  # Still closed, 1 failure recorded
    
    result2 = gateway.complete("request 2")
    assert primary_breaker.state == CircuitState.OPEN  # Opened after 2nd failure
    
    # Verify primary circuit is open
    assert primary_breaker.failure_count == 2
    
    # Next request should skip primary (circuit open) and use backup
    result3 = gateway.complete("request 3")
    assert result3.route.startswith("fallback:backup")  # Should use backup fallback
    assert result3.text  # Should return a valid response
    
    # Verify transition log shows CLOSED -> OPEN
    transitions = primary_breaker.transition_log
    assert len(transitions) >= 1
    assert transitions[-1]["to"] == "open"
    assert transitions[-1]["reason"] == "failure_threshold"
