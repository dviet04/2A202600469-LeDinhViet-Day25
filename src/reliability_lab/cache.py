from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    TODO(student): Add a better semantic similarity function and false-hit guardrails.
    Use the module-level _is_uncacheable() and _looks_like_false_hit() helpers in your
    get() and set() methods.  For production, replace with SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response with privacy and false-hit guardrails."""
        # Privacy check: skip cache for sensitive queries
        if _is_uncacheable(query):
            return None, 0.0
        
        best_value: str | None = None
        best_score = 0.0
        best_key: str | None = None
        now = time.time()
        
        # Clean up expired entries
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        
        # Find best matching cached entry
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key
        
        # Check if best match is above threshold
        if best_score >= self.similarity_threshold:
            # False-hit detection: reject if 4-digit numbers differ (e.g., different years)
            if best_key and _looks_like_false_hit(query, best_key):
                return None, best_score
            return best_value, best_score
        
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response with privacy check."""
        # Skip caching privacy-sensitive queries
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Improved similarity using token overlap + character n-gram overlap.
        
        Combines two metrics:
        1. Token Jaccard: overlap of lowercase words
        2. Character 3-gram Jaccard: overlap of character sequences
        
        This catches semantic similarity better than token alone, e.g.:
        - "refund policy 2024" vs "refund policy 2026" → different because of "2024" vs "2026"
        - "what is your refund policy?" vs "refund policies" → similar despite word differences
        """
        a_lower = a.lower()
        b_lower = b.lower()
        
        if not a_lower or not b_lower:
            return 0.0
        
        # Token Jaccard
        tokens_a = set(a_lower.split())
        tokens_b = set(b_lower.split())
        token_sim = 0.0
        if tokens_a or tokens_b:
            token_sim = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
        
        # Character 3-gram Jaccard
        ngrams_a = set(a_lower[i:i+3] for i in range(len(a_lower) - 2))
        ngrams_b = set(b_lower[i:i+3] for i in range(len(b_lower) - 2))
        ngram_sim = 0.0
        if ngrams_a or ngrams_b:
            ngram_sim = len(ngrams_a & ngrams_b) / len(ngrams_a | ngrams_b)
        
        # Weighted average: 70% token, 30% n-gram
        return 0.7 * token_sim + 0.3 * ngram_sim


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis with guardrails.
        
        Steps:
        1. Return (None, 0.0) if privacy-sensitive
        2. Try exact match via hash lookup
        3. Fall back to similarity scan across all cached keys
        4. Apply false-hit detection before returning
        """
        # Step 1: Privacy check
        if _is_uncacheable(query):
            return None, 0.0
        
        # Step 2: Try exact match first
        exact_key = f"{self.prefix}{self._query_hash(query)}"
        try:
            response = self._redis.hget(exact_key, "response")
            if response is not None:
                return response, 1.0
        except Exception:
            # Redis unavailable or error
            return None, 0.0
        
        # Step 3: Similarity scan across all cached keys
        best_value: str | None = None
        best_score = 0.0
        best_cached_query: str | None = None
        
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_data = self._redis.hgetall(key)
                if not cached_data or "query" not in cached_data:
                    continue
                
                cached_query = cached_data["query"]
                score = ResponseCache.similarity(query, cached_query)
                
                if score > best_score:
                    best_score = score
                    best_value = cached_data.get("response")
                    best_cached_query = cached_query
        except Exception:
            # Redis unavailable or error
            return None, 0.0
        
        # Step 4: Apply threshold and false-hit detection
        if best_score >= self.similarity_threshold:
            if best_cached_query and _looks_like_false_hit(query, best_cached_query):
                self.false_hit_log.append({
                    "query": query,
                    "cached_query": best_cached_query,
                    "score": best_score,
                    "reason": "different_4digit_numbers"
                })
                return None, best_score
            return best_value, best_score
        
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL and privacy check.
        
        Steps:
        1. Skip if privacy-sensitive
        2. Build Redis key from query hash
        3. Store as Redis Hash with TTL
        """
        # Step 1: Privacy check
        if _is_uncacheable(query):
            return
        
        # Step 2: Build key
        key = f"{self.prefix}{self._query_hash(query)}"
        
        # Step 3: Store and set TTL
        try:
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            # Redis unavailable or error - silently fail (graceful degradation)
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
