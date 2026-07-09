# Merchant Module — Critical Rate Limiting & Multi-Agent Fix

## Executive Summary

**URGENCY**: The current approach of binary exponential backoff after receiving 429 errors is fundamentally broken. Multiple agents are hammering z.ai independently, causing cascading failures. This requires immediate intervention before the system becomes completely unusable.

## Root Cause Analysis

### The Problem with Binary Exponential Backoff
```
Current broken approach:
1. Agent 1 → z.ai → 429 error  
2. Agent 1 → wait 5s → retry → 429 error
3. Agent 1 → wait 10s → retry → 429 error
4. Agent 1 → wait 20s → retry → 429 error
5. Agent 1 → wait 40s → retry → 429 error
6. Agent 2 → starts new request → 429 error (worsening the problem)
```

**This is punishing success**: We're penalizing the system for being rate-limited by continuing to hammer it.

### Multiple Independent Agents Issue
Based on your observation about suspending opencode tabs with ctrl-z:

```
The REAL problem:
├── Agent Session 1 (opencode tab 1) → z.ai → 429
├── Agent Session 2 (opencode tab 2) → z.ai → 429  
├── Agent Session 3 (opencode tab 3) → z.ai → 429
├── Agent Session 4 (opencode tab 4) → z.ai → 429
└── Agent Session 5 (background task) → z.ai → 429
```

**All agents are independent and unaware of each other's rate limit status.**

## Immediate Fix: Proxy-Level Rate Limiting

### 1. **Internal Rate Limit Tracking**
Replace the current "wait and retry" approach with "predict and prevent":

```bash
# ~/.hermes/profiles/manager/scripts/proxy-rate-limit-tracker.sh
track_api_limits() {
    local api_key="$1"
    local timestamp=$(date +%s)
    
    # Track requests per key per time window
    sqlite3 ~/.hermes/bot/zai_usage.db << EOF
INSERT OR REPLACE INTO api_rate_limits 
(api_key, window_start_1m, request_count_1m, last_429_ts, backoff_until)
VALUES (
    '$api_key',
    $(date -d @$((timestamp / 60 * 60)) +%s),
    COALESCE((SELECT request_count_1m FROM api_rate_limits 
              WHERE api_key = '$api_key' AND window_start_1m = $(date -d @$((timestamp / 60 * 60)) +%s)), 0) + 1,
    CASE WHEN '$2' = '429' THEN $timestamp 
         ELSE COALESCE((SELECT last_429_ts FROM api_rate_limits 
                       WHERE api_key = '$api_key'), 0) END,
    CASE WHEN '$2' = '429' THEN $(($timestamp + 60))
         ELSE COALESCE((SELECT backoff_until FROM api_rate_limits 
                       WHERE api_key = '$api_key'), 0) END
);
EOF
}

check_rate_limit_before_request() {
    local api_key="$1"
    local result=$(sqlite3 ~/.hermes/bot/zai_usage.db << EOF
SELECT 
    request_count_1m,
    last_429_ts,
    backoff_until,
    CASE WHEN backoff_until > $(date +%s) THEN 'BLOCKED'
         WHEN request_count_1m > 60 THEN 'THROTTLE'
         ELSE 'ALLOW' END as status
FROM api_rate_limits 
WHERE api_key = '$api_key'
ORDER BY window_start_1m DESC LIMIT 1;
EOF
)
    echo "$result"
}
```

### 2. **Intelligent Key Selection**
Instead of binary backoff, implement smart key rotation:

```python
# ~/.hermes/bot/intelligent_key_selector.py
class IntelligentKeySelector:
    def __init__(self):
        self.keys = {
            'ours': {'limit_1m': 60, 'priority': 1},
            'friend': {'limit_1m': 60, 'priority': 2}, 
            'ppq': {'limit_1m': 30, 'priority': 3}  # Lower limit for paid
        }
        
    def select_key(self, required_tokens=0):
        """Select optimal key based on rate limits and quotas"""
        available_keys = []
        
        for key_name, key_info in self.keys.items():
            status = self.check_key_status(key_name)
            if status == 'ALLOW':
                available_keys.append((key_name, key_info['priority']))
            elif status == 'THROTTLE' and required_tokens < 1000:
                # Allow small requests when throttled
                available_keys.append((key_name, key_info['priority'] + 10))
        
        # Sort by priority (lower is better)
        available_keys.sort(key=lambda x: x[1])
        return available_keys[0][0] if available_keys else 'BLOCKED'
    
    def apply_backoff_strategy(self, key_name, status_code):
        """Apply intelligent backoff - NOT binary exponential"""
        if status_code != 429:
            return  # No backoff needed
            
        current_time = time.time()
        if key_name == 'ours':
            # Our key: 30 second progressive backoff
            backoff_time = 30
        elif key_name == 'friend':
            # Friend key: 45 second backoff  
            backoff_time = 45
        else:
            # PPQ: 60 second backoff (paid but sensitive)
            backoff_time = 60
            
        # Set backoff period
        self.set_backoff(key_name, current_time + backoff_time)
```

### 3. **Multi-Agent Coordination**
The critical fix: Make all agents aware of shared rate limits:

```bash
# ~/.hermes/profiles/manager/scripts/shared-rate-limit-lock.sh
acquire_rate_limit_lock() {
    local agent_id="$1"
    local max_wait_seconds=10
    
    # Create shared lock file
    local lock_file="/tmp/zai_rate_limit_lock"
    local start_time=$(date +%s)
    
    while [[ $(date +%s) -lt $((start_time + max_wait_seconds)) ]]; do
        if (set -o noclobber; echo "$agent_id $(date +%s)" > "$lock_file") 2>/dev/null; then
            # Lock acquired
            echo "LOCK_ACQUIRED"
            return 0
        fi
        sleep 0.1
    done
    
    echo "LOCK_TIMEOUT"
    return 1
}

release_rate_limit_lock() {
    rm -f "/tmp/zai_rate_limit_lock"
}
```

## Implementation Plan

### Phase 0: EMERGENCY STABILIZATION (Today)

#### 0.1 **Immediate Agent Coordination**
**Action**: Make all agents check shared rate limit status BEFORE making requests

```bash
# Add to ALL agent scripts before API calls:
if ! check_shared_rate_limits; then
    sleep $(calculate_safe_wait_time)
    return "RATE_LIMITED"
fi
```

#### 0.2 **Proxy Rate Limiting Implementation**
**Action**: Update proxy to track and enforce rate limits

```python
# Add to proxy server (e.g., nginx or custom proxy):
@app.before_request
def rate_limit_check():
    api_key = request.headers.get('Authorization')
    if is_rate_limited(api_key):
        return jsonify({'error': 'Rate limit exceeded'}), 429
```

### Phase 1: Multi-Agent Coordination System (This Week)

#### 1.1 **Shared Rate Limit Database**
**File**: `~/.hermes/bot/zai_usage.db` (extend existing)

```sql
CREATE TABLE IF NOT EXISTS api_rate_limits (
    api_key TEXT PRIMARY KEY,
    window_start_1m INTEGER,
    request_count_1m INTEGER,
    last_429_ts INTEGER,
    backoff_until INTEGER,
    consecutive_429_count INTEGER
);
```

#### 1.2 **Agent Registration System**
**Action**: Register all active agents and coordinate their requests

```bash
# ~/.hermes/profiles/manager/scripts/agent-coordinator.sh
register_agent() {
    local agent_id="$1"
    local capabilities="$2"  # "chat", "code", "search", etc.
    
    sqlite3 ~/.hermes/bot/zai_usage.db << EOF
INSERT OR REPLACE INTO active_agents 
(agent_id, capabilities, last_seen_ts, status)
VALUES ('$agent_id', '$capabilities', $(date +%s), 'active');
EOF
}

get_agent_request_allowance() {
    local agent_id="$1"
    # Check if this agent should make requests based on:
    # 1. Global rate limit status
    # 2. Agent's recent success rate
    # 3. Agent's priority level
}
```

### Phase 2: Intelligent Proxy Enhancement (Next Week)

#### 2.1 **Smart Proxy with Key Rotation**
**Action**: Replace dumb proxy with intelligent request routing

```python
class IntelligentProxy:
    def __init__(self):
        self.key_selector = IntelligentKeySelector()
        self.rate_tracker = RateLimitTracker()
    
    def route_request(self, request):
        # 1. Check global rate limits
        if self.rate_tracker.global_limit_exceeded():
            return Response("Global rate limit exceeded", 429)
        
        # 2. Select optimal API key
        api_key = self.key_selector.select_key()
        
        # 3. Apply request shaping if needed
        if self.rate_tracker.should_throttle(api_key):
            time.sleep(self.rate_tracker.calculate_delay())
        
        # 4. Forward request to selected key
        return self.forward_to_key(request, api_key)
```

#### 2.2 **Multi-Machine Load Balancing**
**Action**: Distribute requests across DQ05 and other machines

```python
class DistributedRequestRouter:
    def __init__(self):
        self.machines = {
            'main': {'capacity': 100, 'current_load': 75},
            'dq05': {'capacity': 80, 'current_load': 40},  # From your SSH data
            'firecracker-vm1': {'capacity': 60, 'current_load': 20},
            'firecracker-vm2': {'capacity': 60, 'current_load': 20}
        }
    
    def select_machine_for_request(self, request_type):
        # Select machine based on:
        # 1. Current load
        # 2. Machine capabilities  
        # 3. Network latency
        # 4. Cost efficiency
        return self.optimal_machine(request_type)
```

## DQ05 Integration Details

### Current DQ05 Specifications (from SSH)
```yaml
dq05_proplus:
  hostname: c03rad0r-DQ05proplus
  cpu: Intel(R) N95 (4 cores)
  memory: 10GB (7.1GB available) 
  storage: 468GB (215GB available)
  load: 3.55, 1.72, 1.52 (currently under moderate load)
  os: Ubuntu 6.17.0-35-generic
  uptime: 17 days
  location: 100.90.22.201 (edge network)
  capabilities: ["edge_computing", "distributed_processing", "backup_node"]
```

### Immediate DQ05 Integration Tasks
1. **Add DQ05 to distributed machine inventory** (kanban task already created)
2. **Deploy resource monitoring agent to DQ05**
3. **Test cross-machine request routing**
4. **Configure DQ05 as backup node for critical services**

## Success Metrics (Immediate)

### Short-term (24 hours)
- [ ] **Zero 429 errors** from coordinated agent requests
- [ ] **Intelligent key selection** working (no more hammering)
- [ ] **Shared rate limit awareness** across all agents
- [ ] **DQ05 connectivity established** and monitored

### Medium-term (1 week)
- [ ] **Multi-agent coordination system** operational
- [ ] **Intelligent proxy** with key rotation
- [ ] **DQ05 integrated** as distributed resource
- [ ] **90% reduction** in unexpected 429 errors

## Files to Modify Immediately

### Critical Files
1. **`~/.hermes/profiles/manager/scripts/safe-fips-dispatch-gate.sh`** - Add rate limit checks
2. **`~/.hermes/bot/zai_usage.db`** - Add rate limit tracking tables
3. **Proxy configuration** - Add intelligent key selection
4. **Agent session scripts** - Add shared rate limit coordination

### New Files to Create
1. **`~/.hermes/profiles/manager/scripts/rate-limit-coordinator.sh`**
2. **`~/.hermes/bot/intelligent_key_selector.py`**
3. **`~/.hermes/profiles/manager/scripts/dq05-monitor.sh`**

## Emergency Actions Required

### **RIGHT NOW (Today)**
1. **Suspend non-critical agents** to reduce load
2. **Implement shared rate limit lock** immediately
3. **Add rate limit checks** to existing dispatch scripts
4. **Establish DQ05 monitoring** (already have SSH access)

### **Next 24 Hours**
1. **Deploy intelligent key selection** 
2. **Create agent coordination system**
3. **Test multi-machine routing to DQ05**
4. **Monitor for 429 error elimination**

---

**Priority: CRITICAL - This affects the basic usability of the entire system**

*Created: 2026-07-08 16:52 UTC*
*Status: Immediate Action Required*