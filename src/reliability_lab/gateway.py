from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.cumulative_cost = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response through cache, breakers, and fallback chain.
        
        Routing logic:
        1. Try cache (if enabled and hit)
        2. Iterate providers respecting circuit breaker state and cost budget
        3. Return static fallback if all providers fail
        
        Route reasons include: cache_hit:{score}, primary:{provider}, fallback:{provider}
        Measures total latency including routing overhead.
        """
        start_time = time.perf_counter()
        
        # Step 1: Try cache first
        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                latency_ms = (time.perf_counter() - start_time) * 1000
                return GatewayResponse(cached, f"cache_hit:{score:.2f}", None, True, latency_ms, 0.0)

        last_error: str | None = None
        
        # Step 2: Try providers in fallback chain
        for provider in self.providers:
            breaker = self.breakers[provider.name]
            
            # Skip if circuit is open
            if breaker.state.value == "open":
                last_error = f"circuit_open:{provider.name}"
                continue
            
            # Skip expensive providers if cost budget exceeded
            if self.cost_budget is not None and self.cumulative_cost >= self.cost_budget:
                last_error = f"cost_budget_exceeded"
                continue
            
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                
                # Cache successful response
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                
                # Track cumulative cost
                self.cumulative_cost += response.estimated_cost
                
                # Determine route reason with provider name
                route_reason = "primary" if provider == self.providers[0] else "fallback"
                route = f"{route_reason}:{provider.name}"
                
                # Add routing overhead to latency
                total_latency_ms = response.latency_ms + (time.perf_counter() - start_time) * 1000
                
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=total_latency_ms,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

        # Step 3: All providers failed — return static fallback
        total_latency_ms = (time.perf_counter() - start_time) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=total_latency_ms,
            estimated_cost=0.0,
            error=last_error,
        )
