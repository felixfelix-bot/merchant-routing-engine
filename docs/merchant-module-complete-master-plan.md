# Merchant Module — Complete Master Plan with LLM Benchmarks & Multi-Provider Pricing

## Executive Summary

This comprehensive plan integrates all merchant module components: emergency rate limiting fixes, distributed resource optimization, LLM benchmark data integration, and multi-provider pricing optimization. The plan includes clearly scoped tasks ready for immediate scheduling.

## Current Status Summary

### ✅ Completed & Operational
- **DQ05 Mini PC**: Intel N95, 10GB RAM, actively running Hermes agents
- **Rate Limiting Analysis**: Identified 429 cascading failures from multiple agents
- **PPQ Bleed Resolution**: Fixed API key and fallback ordering issues
- **Kanban Board Setup**: All tasks structured and ready for scheduling

### 🔥 Emergency Tasks Already Claimed
- **t_96f6b20a**: EMERGENCY: Implement Shared Rate Limit Tracking
- **t_1ef2dcf3**: EMERGENCY: Deploy Intelligent Key Selection  
- **t_f29e95ef**: EMERGENCY: Multi-Agent Coordination System

### 🔄 Research In Progress
- **deleg_c6600e01**: LLM benchmarking databases integration with Kalman filters
- **deleg_820e1cb1**: Multi-provider pricing analysis (ppq.ai vs openrouter)

---

## Phase 0: Emergency Stabilization (Priority 0 - Start Today)

### Task 0.1: EMERGENCY: Implement Shared Rate Limit Tracking
**Kanban**: t_96f6b20a (CLAIMED)
**Timeline**: 4 hours
**Dependencies**: None

#### Implementation
```sql
CREATE TABLE api_rate_limits (
    api_key TEXT PRIMARY KEY,
    window_start_1m INTEGER,
    request_count_1m INTEGER,
    last_429_ts INTEGER,
    backoff_until INTEGER,
    consecutive_429_count INTEGER,
    provider TEXT,
    model TEXT,
    cost_per_1k_tokens REAL
);
```

#### Success Criteria
- [ ] All API requests check rate limits before execution
- [ ] Database tracks rate limit status per API key
- [ ] Zero 429 errors from coordinated requests

### Task 0.2: EMERGENCY: Deploy Intelligent Key Selection
**Kanban**: t_1ef2dcf3 (CLAIMED)  
**Timeline**: 4 hours
**Dependencies**: Task 0.1

#### Implementation
```python
class IntelligentKeySelector:
    def __init__(self):
        self.keys = {
            'ours': {'limit_1m': 60, 'priority': 1, 'cost_factor': 1.0},
            'friend': {'limit_1m': 60, 'priority': 2, 'cost_factor': 1.0},
            'ppq': {'limit_1m': 30, 'priority': 3, 'cost_factor': 1.2},
            'openrouter': {'limit_1m': 45, 'priority': 4, 'cost_factor': 0.9}
        }
    
    def select_key(self, request_type, required_tokens=0):
        """Select optimal key based on rate limits and cost"""
        available_keys = []
        for key_name, key_info in self.keys.items():
            status = self.check_key_status(key_name)
            if status == 'ALLOW':
                # Calculate cost-performance score
                score = self.calculate_key_score(key_name, request_type)
                available_keys.append((key_name, score))
        
        # Sort by score (lower is better)
        available_keys.sort(key=lambda x: x[1])
        return available_keys[0][0] if available_keys else 'BLOCKED'
```

#### Success Criteria
- [ ] Keys selected intelligently based on rate limits and cost
- [ ] Binary exponential backoff eliminated
- [ ] Graduated backoff periods applied (30s/45s/60s)
- [ ] Free keys prioritized over paid keys

### Task 0.3: EMERGENCY: Multi-Agent Coordination System
**Kanban**: t_f29e95ef (CLAIMED)
**Timeline**: 8 hours
**Dependencies**: Tasks 0.1, 0.2

#### Implementation
```bash
# ~/.hermes/profiles/manager/scripts/agent-coordinator.sh
register_agent() {
    local agent_id="$1"
    local capabilities="$2"
    local max_rpm="$3"  # Requests per minute
    
    sqlite3 ~/.hermes/bot/zai_usage.db << EOF
INSERT OR REPLACE INTO active_agents (
    agent_id, capabilities, max_rpm, current_rpm, 
    last_seen_ts, status, priority_level
) VALUES (
    '$agent_id', '$capabilities', $max_rpm, 0,
    $(date +%s), 'active', 5
);
EOF
}

coordinate_requests() {
    local agent_id="$1"
    local required_tokens="$2"
    
    # Check if agent can make requests
    local current_rpm=$(sqlite3 ~/.hermes/bot/zai_usage.db \
        "SELECT current_rpm FROM active_agents WHERE agent_id='$agent_id'")
    
    local max_rpm=$(sqlite3 ~/.hermes/bot/zai_usage.db \
        "SELECT max_rpm FROM active_agents WHERE agent_id='$agent_id'")
    
    if [[ $current_rpm -ge $max_rpm ]]; then
        echo "RATE_LIMITED"
        return 1
    fi
    
    # Increment request count
    sqlite3 ~/.hermes/bot/zai_usage.db \
        "UPDATE active_agents SET current_rpm = current_rpm + 1 WHERE agent_id='$agent_id'"
    
    echo "ALLOWED"
    return 0
}
```

#### Success Criteria
- [ ] Zero independent API hammering across all agents
- [ ] Global request rate limits respected
- [ ] Agent priority levels enforced
- [ ] Emergency agent suspension working (ctrl-z)

---

## Phase 1: Distributed Resource Optimization (Priority 1 - This Week)

### Task 1.1: Phase 2.1.1: Create Distributed Machine Inventory
**Kanban**: t_f452df30 (READY)
**Timeline**: 6 hours
**Dependencies**: None

#### Implementation
```sql
CREATE TABLE distributed_machines (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    type TEXT CHECK (type IN ('firecracker-vm', 'mini-pc', 'vps', 'physical')),
    hostname TEXT,
    ssh_host TEXT,
    ssh_port INTEGER DEFAULT 22,
    cpu_cores INTEGER,
    memory_gb REAL,
    storage_gb REAL,
    cpu_load_1m REAL,
    memory_used_percent REAL,
    capabilities TEXT,  -- JSON array
    active BOOLEAN DEFAULT true,
    last_seen_ts INTEGER,
    location TEXT,     -- "local", "edge", "cloud", "datacenter"
    role TEXT,          -- "compute", "storage", "network", "edge"
    cost_per_hour_sats REAL DEFAULT 0,
    network_latency_ms REAL DEFAULT 0
);
```

#### Success Criteria
- [ ] DQ05 specifications documented in database
- [ ] Firecracker VMs discovered and inventoried
- [ ] Machine connectivity established and tested
- [ ] Resource monitoring deployed to all machines

### Task 1.2: Extend System Resource Collection for Distributed Systems
**Kanban**: New task to create
**Timeline**: 4 hours
**Dependencies**: Task 1.1

#### Implementation
```bash
# Enhanced system-resource-collector.sh for distributed machines
collect_distributed_resources() {
    local machines=$(sqlite3 ~/.hermes/bot/zai_usage.db \
        "SELECT name, ssh_host FROM distributed_machines WHERE active = 1")
    
    for machine_info in $machines; do
        local machine_name=$(echo $machine_info | cut -d'|' -f1)
        local ssh_host=$(echo $machine_info | cut -d'|' -f2)
        
        if ssh_alive $ssh_host; then
            # Collect CPU, memory, network from remote machine
            ssh $ssh_host "cat /proc/loadavg | head -1" > /tmp/${machine_name}_load
            ssh $ssh_host "free -m | grep Mem:" > /tmp/${machine_name}_mem
            ssh $ssh_host "cat /proc/net/dev | grep -E '(eth0|ens33)'" > /tmp/${machine_name}_net
            
            # Store in database with machine identification
            store_distributed_metrics $machine_name
        fi
    done
}
```

#### Success Criteria
- [ ] All distributed machines monitored every 5 minutes
- [ ] Resource data flowing to central database
- [ ] Machine identification in all metrics
- [ ] Remote collection scripts working

### Task 1.3: Create Crash Detection and Analysis Script
**Kanban**: t_24467797 (READY)
**Timeline**: 6 hours  
**Dependencies**: Task 1.2

#### Success Criteria
- [ ] Crashes logged with resource context
- [ ] Crash patterns automatically identified
- [ ] Database stores crash history for analysis
- [ ] Correlation with resource levels working

---

## Phase 2: Dashboard Enhancement (Priority 2 - Next Week)

### Task 2.1: Add System Resource Overview Charts
**Kanban**: t_e5bb1f06 (READY)
**Timeline**: 8 hours
**Dependencies**: Task 1.2 (data collection)

#### Implementation Features
- **Multi-Machine CPU Load**: Logarithmic scale for 0.5→38.0 range
- **Distributed Memory Usage**: Percentage across all machines  
- **Worker Count**: Stepped display per machine
- **Proper Axis Labels**: "CPU Load (processes per core)", "Memory Used (%)", "Worker Count"

#### Success Criteria
- [ ] System resources visible in dashboard
- [ ] Logarithmic scaling for CPU load implemented
- [ ] Proper axis labels and units showing
- [ ] Multi-machine data consolidated

### Task 2.2: Create Resource Violations Heatmap
**Kanban**: t_8385dca7 (READY)  
**Timeline**: 6 hours
**Dependencies**: Tasks 1.2, 1.3

#### Success Criteria
- [ ] Resource violations visible as stacked bar chart
- [ ] Logarithmic scaling for high violation counts
- [ ] Correlation with actual crashes shown
- [ ] Interactive filtering and drill-down working

### Task 2.3: Enhanced Business Decision Metrics
**Kanban**: t_774ff558 (READY)
**Timeline**: 8 hours
**Dependencies**: Tasks 2.1, 2.2

#### Success Criteria
- [ ] Business decision metrics clearly visualized
- [ ] Crash impact on business outcomes shown
- [ ] Logarithmic pricing plots with proper labels
- [ ] Real-time cost efficiency tracking

---

## Phase 3: LLM Benchmark Integration (Priority 3 - Week 2-3)

### Task 3.1: Research LLM Benchmark Databases
**Kanban**: New task - depends on deleg_c6600e01 research
**Timeline**: 8 hours
**Dependencies**: deleg_c6600e01 completion

#### Implementation Plan
```sql
CREATE TABLE llm_benchmarks (
    id INTEGER PRIMARY KEY,
    model_name TEXT,
    provider TEXT,
    benchmark_type TEXT CHECK (benchmark_type IN 
        ('HELM', 'MMLU', 'GSM8K', 'HumanEval', 'BigBenchHard')),
    score REAL,
    max_score REAL,
    percentile_rank REAL,
    test_date INTEGER,
    benchmark_version TEXT
);

CREATE TABLE benchmark_correlations (
    model_name TEXT,
    provider TEXT,
    benchmark_type TEXT,
    cost_per_1k_tokens REAL,
    performance_score REAL,
    efficiency_score REAL,  # performance / cost
    last_updated INTEGER,
    PRIMARY KEY (model_name, provider, benchmark_type)
);
```

#### Success Criteria
- [ ] Benchmark data collection system implemented
- [ ] Correlation with performance/cost established
- [ ] Database populated with benchmark metrics
- [ ] Integration with Kalman filters designed

### Task 3.2: Integrate Benchmarks with Kalman Filters
**Kanban**: New task - Phase 4 extension
**Timeline**: 12 hours
**Dependencies**: Tasks 3.1, 4.1

#### Implementation
```python
class BenchmarkEnhancedKalmanPredictor:
    def __init__(self):
        # Extended state: tokens, cpu_load, memory_pct, worker_count, benchmark_performance
        self.state = np.array([1000.0, 0.5, 50.0, 2.0, 0.8])
        
        # Benchmark-informed measurement noise
        self.R = np.diag([
            1000000.0,    # Token variance
            0.1,          # CPU load variance  
            1.0,          # Memory variance
            0.01,         # Worker count variance
            0.05          # Benchmark performance variance
        ])
    
    def predict_with_benchmarks(self, model_selection, task_requirements):
        """Use benchmark data to improve model selection predictions"""
        benchmark_data = self.get_benchmark_scores(model_selection)
        cost_data = self.get_model_costs(model_selection)
        
        # Calculate efficiency score (performance/cost)
        efficiency_scores = {}
        for model in model_selection:
            perf_score = benchmark_data.get(model, 0.5)
            cost_score = cost_data.get(model, 1.0)
            efficiency_scores[model] = perf_score / cost_score
        
        # Select model with best efficiency
        selected_model = max(efficiency_scores, key=efficiency_scores.get)
        return selected_model, efficiency_scores
```

#### Success Criteria
- [ ] Kalman filters enhanced with benchmark data
- [ ] Model selection predictions improved
- [ ] Performance/cost correlations established
- [ ] Benchmark-driven decisions working

### Task 3.3: Create LLM Performance Dashboard
**Kanban**: New task
**Timeline**: 8 hours
**Dependencies**: Task 3.1

#### Dashboard Components
- **Benchmark Scores Chart**: Model performance across different benchmarks
- **Cost-Performance Ratio**: Efficiency heatmap
- **Provider Comparison**: Same model across different providers
- **Optimization Recommendations**: Best value models per task type

#### Success Criteria
- [ ] LLM performance visualization complete
- [ ] Cost-performance ratios displayed
- [ ] Provider comparisons working
- [ ] Optimization recommendations generated

---

## Phase 4: Multi-Provider Pricing Optimization (Priority 3 - Week 2-3)

### Task 4.1: Research Multi-Provider Pricing Differences
**Kanban**: New task - depends on deleg_820e1cb1 research  
**Timeline**: 8 hours
**Dependencies**: deleg_820e1cb1 completion

#### Implementation Plan
```sql
CREATE TABLE provider_pricing (
    id INTEGER PRIMARY KEY,
    provider_name TEXT,
    model_name TEXT,
    input_price_per_1k REAL,
    output_price_per_1k REAL,
    context_window INTEGER,
    max_tokens_per_minute INTEGER,
    currency TEXT DEFAULT 'USD',
    last_updated INTEGER,
    pricing_tier TEXT CHECK (pricing_tier IN ('free', 'hobby', 'pro', 'enterprise'))
);

CREATE TABLE provider_comparison (
    model_name TEXT,
    provider1 TEXT,
    provider2 TEXT,
    price_difference_percent REAL,
    performance_difference_percent REAL,
    recommended_provider TEXT,
    recommendation_reason TEXT,
    last_updated INTEGER,
    PRIMARY KEY (model_name, provider1, provider2)
);
```

#### Success Criteria
- [ ] All major providers pricing data collected
- [ ] Price differences calculated and stored
- [ ] Performance comparisons established
- [ ] Database populated with pricing information

### Task 4.2: Implement Intelligent Provider Selection
**Kanban**: New task
**Timeline**: 12 hours
**Dependencies**: Tasks 4.1, 3.1

#### Implementation
```python
class IntelligentProviderSelector:
    def __init__(self):
        self.providers = ['ppq', 'openrouter', 'zai', 'anthropic', 'openai']
        self.benchmark_weights = {
            'MMLU': 0.3,      # Reasoning ability
            'GSM8K': 0.2,     # Math ability  
            'HumanEval': 0.2, # Code ability
            'HELM': 0.3       # Overall capability
        }
    
    def select_optimal_provider(self, model_name, task_type, budget_limit):
        """Select provider based on price, performance, and requirements"""
        # Get pricing data for all providers
        pricing_data = self.get_pricing_data(model_name)
        benchmark_data = self.get_benchmark_data(model_name)
        
        # Calculate value scores (performance / cost)
        value_scores = {}
        for provider in pricing_data:
            cost = pricing_data[provider]['total_cost']
            performance = self.calculate_weighted_score(
                benchmark_data[provider], self.benchmark_weights
            )
            value_score = performance / cost
            value_scores[provider] = value_score
        
        # Apply budget constraints
        affordable_providers = {
            p: score for p, score in value_scores.items()
            if pricing_data[p]['total_cost'] <= budget_limit
        }
        
        if not affordable_providers:
            # Find cheapest option that meets minimum performance
            return self.find_cheapest_viable_option(pricing_data, benchmark_data)
        
        # Return provider with best value score
        return max(affordable_providers, key=affordable_providers.get)
```

#### Success Criteria
- [ ] Provider selection algorithm implemented
- [ ] Price/performance optimization working
- [ ] Budget constraints enforced
- [ ] Multi-provider routing functional

### Task 4.3: Create Multi-Provider Pricing Dashboard
**Kanban**: New task
**Timeline**: 8 hours
**Dependencies**: Task 4.1

#### Dashboard Components
- **Price Comparison Chart**: Same model across different providers
- **Cost Savings Analysis**: Potential savings from provider switching
- **Performance vs Cost Scatter**: Best value providers highlighted
- **Provider Recommendation Engine**: Real-time optimal provider suggestions

#### Success Criteria
- [ ] Multi-provider pricing visualization complete
- [ ] Cost savings analysis working
- [ ] Performance/cost scatter plots showing
- [ ] Provider recommendations generated

---

## Phase 5: Advanced Distributed Optimization (Priority 4 - Week 3-4)

### Task 5.1: Extend KalmanPredictor for Multi-Resource Prediction
**Kanban**: t_d5601748 (TODO)
**Timeline**: 12 hours
**Dependencies**: Tasks 1.2, 3.1, 4.1

#### Success Criteria
- [ ] Kalman filter tracks all system resources
- [ ] Accurate predictions 30+ minutes ahead
- [ ] Resource correlations modeled
- [ ] Early warning system functional

### Task 5.2: Resource-Aware Dispatch Integration
**Kanban**: t_2d5fd7af (TODO)
**Timeline**: 12 hours  
**Dependencies**: Tasks 5.1, 0.3

#### Success Criteria
- [ ] Dispatch decisions use Kalman predictions
- [ ] 75% reduction in unexpected crashes
- [ ] Optimal resource utilization
- [ ] Self-healing system functional

### Task 5.3: Implement Predictive Crash Prevention
**Kanban**: t_9ce89082 (RUNNING)
**Timeline**: 16 hours
**Dependencies**: Task 5.2

#### Success Criteria
- [ ] 90% of predicted resource exhaustion events prevented
- [ ] 50% reduction in unexpected crashes
- [ ] Accurate 30+ minute predictions
- [ ] Self-healing system fully operational

---

## Phase 6: Complete System Integration (Priority 4 - Week 4)

### Task 6.1: Project Management: Success Metrics & Progress Tracking
**Kanban**: t_936aa3fa (BLOCKED)
**Timeline**: Ongoing
**Dependencies**: All tasks

#### Success Metrics by Week
- **Week 1**: 75% crash reduction, stable agent coordination
- **Week 2**: Dashboard operational, benchmark data flowing
- **Week 3**: Multi-provider pricing optimization working
- **Week 4**: Complete self-healing distributed system

### Task 6.2: System Documentation and Knowledge Base
**Kanban**: New task
**Timeline**: 8 hours
**Dependencies**: All major components complete

#### Success Criteria
- [ ] Complete system documentation created
- [ ] Troubleshooting guide written
- [ ] Best practices documented
- [ ] Knowledge base operational

---

## Task Summary for Immediate Scheduling

### 🚨 Priority 0 (Emergency - Start Today)
- t_96f6b20a: EMERGENCY: Implement Shared Rate Limit Tracking (CLAIMED)
- t_1ef2dcf3: EMERGENCY: Deploy Intelligent Key Selection (CLAIMED)  
- t_f29e95ef: EMERGENCY: Multi-Agent Coordination System (CLAIMED)

### 🔥 Priority 1 (This Week)
- t_f452df30: Create Distributed Machine Inventory (READY)
- [NEW]: Extend System Resource Collection for Distributed Systems
- t_24467797: Create Crash Detection and Analysis Script (READY)

### 📊 Priority 2 (Next Week)
- t_e5bb1f06: Add System Resource Overview Charts (READY)
- t_8385dca7: Create Resource Violations Heatmap (READY)
- t_774ff558: Enhanced Business Decision Metrics (READY)

### 🤖 Priority 3 (Week 2-3)
- [NEW]: Research LLM Benchmark Databases (depends on deleg_c6600e01)
- [NEW]: Integrate Benchmarks with Kalman Filters
- [NEW]: Create LLM Performance Dashboard
- [NEW]: Research Multi-Provider Pricing Differences (depends on deleg_820e1cb1)
- [NEW]: Implement Intelligent Provider Selection
- [NEW]: Create Multi-Provider Pricing Dashboard

### 🔧 Priority 4 (Week 3-4)
- t_d5601748: Extend KalmanPredictor for Multi-Resource Prediction (TODO)
- t_2d5fd7af: Resource-Aware Dispatch Integration (TODO)
- t_9ce89082: Implement Predictive Crash Prevention (RUNNING)
- [NEW]: System Documentation and Knowledge Base

---

## Success Metrics Summary

### Immediate (24 hours)
- **Zero 429 errors** from coordinated agent requests
- **DQ05 integration** complete and monitored
- **Emergency rate limiting** operational

### Short-term (1 week)  
- **75% crash reduction** through intelligent coordination
- **Distributed resources** fully monitored
- **Dashboard visualization** of system resources

### Medium-term (2-3 weeks)
- **LLM benchmark integration** for better model selection
- **Multi-provider pricing** optimization operational
- **Cost-performance** recommendations automated

### Long-term (4 weeks)
- **Self-healing distributed system** fully operational
- **90% crash reduction** achieved through prediction
- **Complete business intelligence** for all resource decisions

---

## Next Steps

1. **APPROVE THIS PLAN**: Review the comprehensive scope and provide approval
2. **SCHEDULE TASKS**: Once approved, I'll schedule all the new tasks on the kanban board
3. **START IMPLEMENTATION**: Begin with Priority 0 emergency tasks
4. **MONITOR PROGRESS**: Regular updates on subagent research findings

**The merchant module now spans from emergency rate limiting fixes to complete LLM optimization with benchmarks and multi-provider pricing!** 🚀

---

*Plan Created: 2026-07-08 17:00 UTC*
*Status: Awaiting Approval*