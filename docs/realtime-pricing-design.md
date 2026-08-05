# Realtime Pricing Design — Per-Endpoint Price Models

## Felix's Vision

Each AI endpoint has its own pricing model. All complexity lives INSIDE the price. The consumer (router) just compares prices and picks the cheapest.

Future: pricing model → Routstr node (publishes price). Consumer → Routstr client (picks min price).

## Current Architecture (already matches vision)

```
┌─────────────────────────────────────────────────────┐
│ ROUTING OPTIMIZER (the "Routstr client")            │
│                                                     │
│  for each provider:                                 │
│    price = provider.compute_effective_price()       │
│  pick provider with min(price)                      │
│                                                     │
│  Simple. No special cases. No thresholds.           │
└─────────────────────────────────────────────────────┘
           ↑ price_per_token
           │
    ┌──────┴──────┬──────────┬──────────┬──────────┐
    │             │          │          │          │
┌───┴───┐   ┌────┴────┐ ┌───┴───┐ ┌────┴────┐ ┌───┴───┐
│ z.ai  │   │ Ollama  │ │ PPQ   │ │ OpenRtr │ │ DeepI │
│ ours  │   │ Cloud   │ │       │ │         │ │ nfra  │
│       │   │         │ │       │ │         │ │       │
│MODEL: │   │MODEL:   │ │MODEL: │ │MODEL:   │ │MODEL: │
│peak   │   │quota    │ │flat   │ │per-call │ │per-   │
│hours  │   │pressure │ │per-   │ │measured │ │call   │
│scarcity│  │+extra   │ │token  │ │cost     │ │cost   │
│health │   │usage    │ │       │ │         │ │       │
│       │   │Kimi=    │ │       │ │         │ │       │
│$0     │   │always$  │ │$0.14  │ │$0.135   │ │~$0.05 │
│marginal│  │extra    │ │/M     │ │/M       │ │/M     │
└───────┘   └─────────┘ └───────┘ └─────────┘ └───────┘
```

## Per-Endpoint Pricing Models

### z.ai (ours + friend)
- **Model:** Flat-rate subscription ($155/mo ours, shared friend)
- **Marginal cost:** $0/token (already paid)
- **Multipliers:** peak_hours (3x during Beijing afternoon), scarcity, health
- **Effective price:** ~$0.001/M (MIN_EFFECTIVE_PRICE floor) * peak * scarcity * health
- **Real measurement:** $155 / monthly_tokens (amortized, for reporting only)

### Ollama Cloud
- **Model:** Subscription ($100/mo) + prepaid extra usage
- **Base rate:** $0.0155/M (measured: $38.51 / 2.3B tokens over 4 weeks)
- **Quota pressure:** As session.usage → 1.0, price ramps up continuously
- **Extra usage:** When usage > 1.0, paying from prepaid at higher effective rate
- **Kimi models:** Always extra (never included in subscription quota)
- **Effective price:**
  - Included (usage < 0.7): $0.0155/M * scarcity * health
  - Transition (usage 0.7-1.0): smooth ramp from $0.0155 to extra_rate
  - Extra (usage > 1.0): extra_rate * scarcity * health
  - Extra rate ≈ $0.05/M (estimated; will be refined by real data)

### PPQ (api.ppq.ai)
- **Model:** Pure pay-per-token
- **Rate:** ~$0.14/M (published, will be replaced by measured from response.cost)
- **No multipliers:** No peak, no scarcity (unlimited capacity, metered)
- **Effective price:** $0.14/M flat (measured once we capture response.cost)

### OpenRouter
- **Model:** Pure pay-per-token
- **Rate:** ~$0.135/M (published)
- **Real measurement:** response.usage.cost returned in every call
- **Effective price:** measured per-call, rolling average

### DeepInfra
- **Model:** Pay-per-token from prepaid balance ($5 starting)
- **Rate:** ~$0.05/M (estimated)
- **Real measurement:** response.usage.estimated_cost + balance tracking
- **Effective price:** measured per-call

## The Key Refactor: EndpointPriceModel class

Currently pricing_engine.py has functions that take provider as parameter.
Felix wants each endpoint to BE a pricing model instance.

```python
class EndpointPriceModel:
    """Base: each endpoint computes its own price. All complexity lives here."""
    
    def compute_price(self, context: PricingContext) -> float:
        """Return $/M for this endpoint right now."""
        raise NotImplementedError

class ZaiPriceModel(EndpointPriceModel):
    def compute_price(self, ctx):
        base = 0.001  # MIN_EFFECTIVE_PRICE (flat-rate, ~$0 marginal)
        peak = peak_multiplier(ctx.hour_utc, self.peak_hours)
        scarcity = scarcity_factor(ctx.quota_pct)
        health = health_pricing_factor(ctx.failures, ctx.breaker)
        return base * peak * scarcity * health

class OllamaCloudPriceModel(EndpointPriceModel):
    def compute_price(self, ctx):
        base = self.real_rate_tracker.get_rate("ollama_cloud", ctx.model)
        # Continuous quota pressure — all complexity here
        quota_mult = self._quota_pressure(ctx.session_usage, ctx.model)
        health = health_pricing_factor(ctx.failures, ctx.breaker)
        return base * quota_mult * health
    
    def _quota_pressure(self, usage_fraction, model):
        """Smooth ramp. Kimi models = always extra."""
        if model.startswith("kimi"):
            return self.extra_rate / self.base_rate  # always extra multiplier
        if usage_fraction < 0.7:
            return 1.0
        if usage_fraction < 1.0:
            t = (usage_fraction - 0.7) / 0.3  # 0 to 1
            return 1.0 + t * ((self.extra_rate / self.base_rate) - 1.0)
        return self.extra_rate / self.base_rate  # at/over limit

class PpqPriceModel(EndpointPriceModel):
    def compute_price(self, ctx):
        return self.real_rate_tracker.get_rate("ppq", ctx.model)  # flat per-token

class OpenRouterPriceModel(EndpointPriceModel):
    def compute_price(self, ctx):
        return self.real_rate_tracker.get_rate("openrouter", ctx.model)
```

The optimizer becomes trivially simple:
```python
class RoutstrClient:  # currently RoutingOptimizer
    def route(self, request):
        prices = {name: model.compute_price(context) 
                  for name, model in self.endpoints.items()}
        return min(prices, key=prices.get)  # pick cheapest
```

## Migration Path (current → future)

1. NOW: Wrap existing pricing_engine functions into EndpointPriceModel subclasses
2. NEXT: Each model gets its own real_rate_tracker feed
3. FUTURE: Each model becomes a Routstr node that publishes its price via Nostr
4. FUTURE: The optimizer becomes a pure Routstr client that subscribes to price events

## Current Task Alignment

RP-PRICING (running now) implements the continuous quota-pressure in the EXISTING architecture.
The EndpointPriceModel refactor is a follow-up — it doesn't change behavior, just reorganizes so each endpoint's pricing is self-contained.
