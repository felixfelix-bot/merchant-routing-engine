# Merchant Module — Master Plan

## Overview
A Kalman filter-based decision engine for optimizing resource allocation and business decisions across multiple constraints. Initially dogfooded on Hermes AI agent and TollGate internet service provider.

## Problem Statement
Current systems face multiple orthogonal resource constraints that impact reliable work:
- **API Cost**: PPQ.AI credit burn, z.ai token optimization  
- **System Resources**: CPU load spikes, memory pressure, swap exhaustion
- **Business Logic**: Optimal upstream selection, task prioritization, scaling decisions

## Scope
- **Hermes Integration**: Resource-aware dispatch gate, cost optimization
- **TollGate Integration**: Upstream provider selection, pricing optimization
- **Dashboard**: Multi-resource monitoring with log scale pricing
- **Core Engine**: Kalman-based prediction and decision logic

## Current Pain Points
1. **Resource Blind Spots**: Dashboard tracks API costs but misses CPU/memory (cron log shows LOAD_TOO_HIGH, MEM_TOO_LOW blocking work)
2. **Crash Cycles**: Workers crash under resource pressure, lose context, burn tokens debugging
3. **Suboptimal Decisions**: Manual intervention needed for resource allocation and prioritization
4. **Single Constraint Focus**: Current Kalman filters optimize for API tokens but ignore system constraints

## Key Design Decisions

### Multi-Resource Kalman Engine
Extend existing Kalman infrastructure to track and predict:
- **API Costs**: Token burn rates, provider pricing, quota windows
- **CPU Load**: Per-core utilization, load averages, thermal throttling
- **Memory**: Available RAM, swap pressure, OOM events
- **Network**: Bandwidth, latency, packet loss (for TollGate)

### Decision-Making Logic
**Resource-Aware Dispatch**:
```
IF (API_quota_available AND CPU_load_safe AND Memory_available) THEN dispatch
ELSE IF (critical_business_task) THEN scale_up OR wait
ELSE defer OR downgrade_model
```

**TollGate Upstream Selection**:
```
SELECT upstream_provider 
WHERE (cost_per_mb < threshold AND reliability > threshold AND latency < threshold)
ORDER BY business_value DESC
```

### Architecture
```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Hermes AI     │    │   Merchant     │    │   TollGate      │
│   Agent         │───▶│   Module Core  │◀───│   Provider      │
│                 │    │                 │    │                 │
│ - API Costs     │    │ - Kalman Engine │    │ - Network Cost   │
│ - CPU/Memory    │    │ - Business Logic│    │ - Reliability   │
│ - Dispatch Gate │    │ - Decision API  │    │ - Pricing        │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │   Dashboard     │
                       │                 │
                       │ - Multi-Resource│
                       │ - Log Scale     │
                       │ - Predictions   │
                       └─────────────────┘
```

## Implementation Phases

### Phase 1 — Core Kalman Extension
**Goal**: Extend existing burn-rate Kalman to track system resources

1. **Update KalmanPredictor class** (`~/.hermes/bot/burn_predictor.py`)
   - Add system resource states (CPU, memory, network)
   - Multi-dimensional state vector: `[tokens, cpu%, memory%, network_latency]`
   - Adaptive process noise per resource type

2. **System Resource Collector** 
   - Extend ContextVM to collect CPU/memory metrics
   - Store in `zai_usage.db` alongside API calls
   - 5-minute collection cron (matches existing pattern)

3. **Multi-Constraint Decision Logic**
   - Resource safety thresholds (configurable)
   - Priority-based resource allocation
   - Fallback strategies when constraints conflict

### Phase 2 — Hermes Integration
**Goal**: Resource-aware dispatch gate for reliable work

1. **Update Safe FIPS Dispatch Gate** (current cron job)
   - Read from multi-resource Kalman predictions
   - Block dispatch when ANY resource constraint violated
   - Auto-scale worker limits based on predictions

2. **Resource-Aware Model Selection**
   - Downgrade to cheaper models under resource pressure
   - Prefer local Ollama vs PPQ when system constrained
   - Queue strategy for critical vs non-critical tasks

3. **Crash Recovery Optimization**
   - Predict resource exhaustion before crashes occur
   - Graceful degradation instead of hard failures
   - Context preservation across restarts

### Phase 3 — TollGate Integration  
**Goal**: Optimize TollGate upstream provider business decisions

1. **Upstream Provider Data Model**
   - Provider attributes: cost, reliability, latency, bandwidth
   - Historical performance tracking
   - Kalman prediction of provider quality

2. **Business Decision Engine**
   - Cost-benefit analysis per provider
   - Optimal routing based on multiple constraints
   - Dynamic pricing based on resource availability

3. **TollGate Module Integration**
   - Integration with `tollgate-module-basic-go`
   - Provider switching logic based on business decisions
   - Real-time pricing adjustments

### Phase 4 — Dashboard Enhancement
**Goal**: Complete multi-resource monitoring with business insights

1. **Extended Dashboard** (`~/nsites/kalman-data/scripts/build_v3.py`)
   - Add CPU/memory resource plots alongside API costs
   - Logarithmic y-axis for pricing (as requested)
   - Multi-resource constraint violations heatmap
   - Business decision impact visualization

2. **Business Metrics**
   - Cost per successful task completion
   - Resource utilization efficiency  
   - Decision accuracy (predicted vs actual outcomes)

3. **Alerting and Recommendations**
   - Resource constraint warnings before blocking
   - Business optimization suggestions
   - Cost-saving opportunities identification

## Current Status

### Completed
- ✅ Basic Kalman burn-rate prediction (API costs)
- ✅ PPQ bleed detection and emergency fixes
- ✅ ContextVM system monitoring foundation
- ✅ Safe FIPS Dispatch Gate (resource-aware, single constraint)

### In Progress  
- 🔄 Multi-resource data collection design
- 🔄 TollGate upstream provider cost analysis
- 🔄 Dashboard architecture planning

### Next Steps
1. **Immediate**: Extend KalmanPredictor for system resources
2. **Short-term**: Phase 1 - Core multi-resource tracking
3. **Medium-term**: Phase 2 - Hermes integration for reliable dispatch
4. **Long-term**: Phase 3 - TollGate business decisions

## Repositories
- **Core**: `merchant-module` (new repo - shared Kalman engine)
- **Hermes Integration**: `hermes-orchestration` 
- **TollGate Integration**: `tollgate-module-basic-go`
- **Dashboard**: `nsites/kalman-data` (existing)

## Key Files to Reference
- `~/.hermes/bot/burn_predictor.py` - Current Kalman implementation
- `~/.hermes/profiles/manager/scripts/safe-fips-dispatch-gate.sh` - Current dispatch logic
- `~/nsites/kalman-data/scripts/build_v3.py` - Dashboard build script
- `~/plans/tollgate-fips-master-plan.md` - TollGate context

## Open Questions
1. **Architecture**: Should we create a standalone `merchant-module` repo or extend existing infrastructure?
2. **Priority**: Focus first on Hermes stability (crash prevention) or TollGate business optimization?
3. **Data Sources**: What additional metrics needed for TollGate provider decisions?
4. **Integration**: How to coordinate decisions between Hermes and TollGate modules?

---

*Last Updated: 2026-07-08*
*Next Review: After Phase 1 completion*