# Merchant Module — Comprehensive Implementation Plan

## Executive Summary

This plan addresses the critical dispatch crash issues and implements a comprehensive dashboard for resource monitoring and business decision visualization. Based on crash analysis, we have systemic issues with resource management, API rate limits, and worker lifecycle that require immediate attention alongside dashboard enhancements.

## Root Cause Analysis Summary

### 1. **API Rate Limit Crashes** (Primary Issue)
**Pattern**: HTTP 429 errors from both z.ai proxy and direct API calls
**Impact**: Workers hang for 40+ seconds, then fail after 6 retries, losing context
**Evidence**: Persistent 429 errors in `~/.hermes/logs/errors.log` across multiple dates
**Root Cause**: Insufficient rate limiting logic, concurrent workers overwhelming quotas

### 2. **Resource Constraint Emergencies** (Systemic Issue)
**Pattern**: "EMERGENCY mem=56% load=0.43 -> max_in_progress=0" oscillations
**Impact**: Worker count violently swings from 0→4→0→1→0→4 causing instability
**Evidence**: `~/.hermes/logs/worker_ramp.log` shows emergency scaling every 10 seconds
**Root Cause**: Memory thresholds too aggressive (56% triggers emergency), no hysteresis

### 3. **Connection Reset Crashes** (Secondary Issue)
**Pattern**: `APIConnectionError: Connection error` from localhost:9099 proxy
**Impact**: Workers crash on proxy restarts/reloads, causing cascading failures
**Evidence**: Connection errors immediately after proxy service operations
**Root Cause**: Proxy restart without graceful worker handling

### 4. **Monitoring Gaps** (Contributing Issue)
**Missing**: Real-time CPU/memory tracking in dashboard, correlation visualization
**Impact**: No early warning of resource exhaustion, poor decision data
**Evidence**: Current dashboard only tracks API costs, no system resources

---

## Phase 1: Immediate Crash Prevention (Priority: CRITICAL)

### 1.1 **Fix API Rate Limit Cascading Failures**
**Timeline**: Immediate (today)
**Files**: `~/.hermes/profiles/manager/scripts/safe-fips-dispatch-gate.sh`

**Problem**: Workers spawn concurrently, hit 429 limits, hang, crash
**Solution**:
```bash
# Implement staggered worker startup with rate limit awareness
MAX_WORKERS_CONCURRENT=2  # Reduce from current to prevent 429 storms
API_RATE_LIMIT_BUFFER=5   # 5 second buffer between worker starts
WORKER_RETRY_DELAY=30    # 30s delay before respawning crashed worker
```

**Implementation**:
1. **Staggered Dispatch**: Add `$API_RATE_LIMIT_BUFFER` sleep between worker spawns
2. **Concurrency Cap**: Reduce max concurrent workers to 2 temporarily  
3. **Backoff Retry**: Implement exponential backoff for crashed workers
4. **Rate Limit Detection**: Add curl test to `localhost:9099/health` before spawning

### 1.2 **Fix Emergency Scaling Oscillations**
**Timeline**: Immediate (today)  
**Files**: `~/.hermes/bot/adaptive_dispatch_kalman.py` (if exists) or dispatch scripts

**Problem**: Memory threshold 56% triggers emergency scaling, causing 0→4→0→4 swings
**Solution**:
```bash
# Implement hysteresis and gradual scaling
EMERGENCY_MEMORY_HIGH=80    # Raise threshold significantly
EMERGENCY_MEMORY_LOW=65     # Add recovery threshold
SCALE_INCREMENT=1           # Only adjust by ±1 at a time
SCALE_COOLDOWN=30           # 30s between scaling decisions
```

**Implementation**:
1. **Hysteresis Bands**: Emergency stops at 80%, resumes at 65% (not 56%→54%→56%)
2. **Gradual Scaling**: Only adjust worker count by ±1 per decision cycle
3. **Cooldown Period**: Minimum 30s between scaling decisions
4. **Memory Pressure Detection**: Use `/proc/meminfo` Available > buff/cache for accuracy

### 1.3 **Implement Graceful Proxy Restarts**
**Timeline**: Immediate (today)
**Files**: `~/.hermes/profiles/manager/scripts/restart-proxy.sh` (create if missing)

**Problem**: Proxy restarts kill active worker connections
**Solution**:
```bash
# Graceful proxy restart procedure
graceful_restart_proxy() {
    # 1. Pause worker spawning
    touch /tmp/proxy_restart_lock
    # 2. Wait for active workers to complete (max 60s)
    wait_for_workers_or_timeout 60
    # 3. Restart proxy
    systemctl restart zai-proxy
    # 4. Verify proxy health
    wait_for_proxy_health 30
    # 5. Resume worker spawning  
    rm /tmp/proxy_restart_lock
}
```

---

## Phase 2: System Resource Data Collection (Priority: HIGH)

### 2.1 **Comprehensive Resource Monitoring Script**
**Timeline**: This week
**File**: `~/.hermes/profiles/manager/scripts/system-resource-collector.sh`

**Collect every 5 minutes**:
```sql
-- New table in zai_usage.db
CREATE TABLE resource_metrics (
    ts INTEGER PRIMARY KEY,
    cpu_load_1m REAL,
    cpu_load_5m REAL,
    cpu_load_15m REAL,
    memory_available_mb INTEGER,
    memory_used_percent INTEGER,
    swap_used_percent INTEGER,
    disk_io_reads INTEGER,
    disk_io_writes INTEGER,
    network_rx_bytes INTEGER,
    network_tx_bytes INTEGER,
    worker_count INTEGER,
    blocked_by_load INTEGER,
    blocked_by_memory INTEGER
);
```

**Implementation**:
```bash
#!/bin/bash
# system-resource-collector.sh
collect_metrics() {
    local ts=$(date +%s)
    
    # CPU loads
    read load1 load5 load15 <<< $(cat /proc/loadavg | awk '{print $1,$2,$3}')
    
    # Memory (available, not just free)
    read mem_total mem_avail <<< $(free -m | awk '/Mem:/ {print $2, $7}')
    mem_used_pct=$((100 - (mem_avail * 100 / mem_total)))
    
    # Swap
    swap_used_pct=$(free | awk '/Swap:/ {print $3/$2*100; if ($2==0) print 0}')
    
    # Disk I/O (since boot)
    read disk_reads disk_writes <<< $(cat /proc/diskstats | awk '/sda/ {print $4, $8}')
    
    # Network
    read rx_bytes tx_bytes <<< $(cat /proc/net/dev | awk '/ens33/ {print $2, $10}')
    
    # Worker state
    worker_count=$(hermes kanban --board fips list --format json | jq '.running | length')
    blocked_load=$(pgrep -f "LOAD_TOO_HIGH" | wc -l)
    blocked_mem=$(pgrep -f "MEM_TOO_LOW" | wc -l)
    
    # Store in database
    sqlite3 ~/.hermes/bot/zai_usage.db << EOF
INSERT INTO resource_metrics VALUES (
    $ts, $load1, $load5, $load15, $mem_avail, $mem_used_pct, 
    $swap_used_pct, $disk_reads, $disk_writes, $rx_bytes, $tx_bytes,
    $worker_count, $blocked_load, $blocked_mem
);
EOF
}
```

### 2.2 **Crash Detection and Analysis Script**
**Timeline**: This week
**File**: `~/.hermes/profiles/manager/scripts/crash-monitor.sh`

**Implementation**:
```bash
#!/bin/bash
# crash-monitor.sh
analyze_crashes() {
    # Parse cron logs for crash patterns
    local crash_count=$(grep -c "Crashed:" ~/.hermes/logs/cron.log)
    local oom_events=$(dmesg | grep -c "Out of memory")
    local rate_limit_errors=$(grep -c "429.*too many requests" ~/.hermes/logs/errors.log)
    
    # Log crash analysis
    echo "[$(date)] Crashes: $crash_count, OOM: $oom_events, RateLimit: $rate_limit_errors" >> \
        ~/.hermes/logs/crash_analysis.log
    
    # Store in database for dashboard
    sqlite3 ~/.hermes/bot/zai_usage.db << EOF
INSERT INTO crash_events (ts, crash_count, oom_count, rate_limit_count) 
VALUES ($(date +%s), $crash_count, $oom_events, $rate_limit_errors);
EOF
}
```

---

## Phase 3: Dashboard Enhancement (Priority: HIGH)

### 3.1 **Current Dashboard Analysis**
**File**: `~/nsites/kalman-data/scripts/build_v3.py`
**Status**: Already has log-scale support for SATs pricing, needs system resources

### 3.2 **Enhanced Dashboard Implementation**

#### **Chart 1: System Resource Overview** (NEW)
```javascript
// Add to build_v3.py - Chart generation
const resourceChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: timestamps,
        datasets: [
            {
                label: 'CPU Load (1m avg)',
                data: cpu_load_1m,
                borderColor: 'rgb(255, 99, 132)',
                yAxisID: 'y-axis-cpu',
                fill: false
            },
            {
                label: 'Memory Used %',
                data: memory_used_percent,
                borderColor: 'rgb(54, 162, 235)',
                yAxisID: 'y-axis-memory',
                fill: false
            },
            {
                label: 'Active Workers',
                data: worker_count,
                borderColor: 'rgb(255, 206, 86)',
                yAxisID: 'y-axis-count',
                stepped: true
            }
        ]
    },
    options: {
        scales: {
            'y-axis-cpu': {
                type: 'linear',
                position: 'left',
                title: { display: true, text: 'CPU Load (processes per core)' },
                min: 0,
                max: 50,  // Log-scale equivalent for CPU
                ticks: { callback: function(value) { return value.toFixed(1); } }
            },
            'y-axis-memory': {
                type: 'linear',
                position: 'right',
                title: { display: true, text: 'Memory Used (%)' },
                min: 0,
                max: 100
            },
            'y-axis-count': {
                type: 'linear',
                position: 'right',
                title: { display: true, text: 'Worker Count' },
                min: 0,
                max: 50,
                ticks: { stepSize: 1 }
            }
        }
    }
});
```

#### **Chart 2: Resource Violations Heatmap** (NEW)
```javascript
// Real-time constraint violations
const violationChart = new Chart(ctx, {
    type: 'bar',
    data: {
        labels: timestamps,
        datasets: [
            {
                label: 'Load Too High',
                data: blocked_by_load,
                backgroundColor: 'rgba(255, 99, 132, 0.8)',
                stack: 'violations'
            },
            {
                label: 'Memory Too Low', 
                data: blocked_by_memory,
                backgroundColor: 'rgba(54, 162, 235, 0.8)',
                stack: 'violations'
            },
            {
                label: 'Rate Limited',
                data: rate_limit_errors,
                backgroundColor: 'rgba(255, 206, 86, 0.8)',
                stack: 'violations'
            }
        ]
    },
    options: {
        scales: {
            y: {
                type: 'logarithmic',  // Log scale for high dynamic range
                title: { display: true, text: 'Violation Count (log scale)' },
                min: 1
            }
        },
        plugins: {
            title: {
                display: true,
                text: 'Resource Constraint Violations (Last 24 Hours)'
            }
        }
    }
});
```

#### **Chart 3: Business Decision Metrics** (ENHANCED)
```javascript
// Enhanced SATs burned chart with crash correlation
const businessChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: timestamps,
        datasets: [
            {
                label: 'SATs Burned (Log Scale)',
                data: sats_burned_hourly,
                borderColor: 'rgb(75, 192, 192)',
                fill: false,
                yAxisID: 'y-axis-sats'
            },
            {
                label: 'Tasks Completed',
                data: tasks_completed,
                borderColor: 'rgb(153, 102, 255)',
                yAxisID: 'y-axis-count',
                fill: false
            },
            {
                label: 'Crashes (Right Axis)',
                data: crash_count,
                borderColor: 'rgb(255, 99, 132)',
                yAxisID: 'y-axis-crashes',
                stepped: true
            }
        ]
    },
    options: {
        scales: {
            'y-axis-sats': {
                type: 'logarithmic',
                position: 'left',
                title: { display: true, text: 'SATs Burned/Hour (log scale)' },
                min: 1,
                ticks: { callback: function(value) { return value.toLocaleString() + ' SATs'; } }
            },
            'y-axis-count': {
                type: 'linear',
                position: 'right',
                title: { display: true, text: 'Tasks Completed' },
                min: 0
            },
            'y-axis-crashes': {
                type: 'linear',
                position: 'right',
                title: { display: true, text: 'Crashes' },
                min: 0,
                ticks: { stepSize: 1 }
            }
        },
        plugins: {
            tooltip: {
                callbacks: {
                    afterLabel: function(context) {
                        return 'Cost per task: ' + 
                            (context.parsed.y / context.dataset.data[context.dataIndex]).toFixed(0) + 
                            ' SATs/task';
                    }
                }
            }
        }
    }
});
```

### 3.3 **Dashboard Data Structure Extension**
**Add to build_v3.py**:
```python
# Extended data.json structure
{
    "resource_metrics": {
        "timestamps": [ts1, ts2, ...],
        "cpu_load_1m": [0.35, 0.72, ...],
        "memory_used_percent": [53, 58, ...],
        "worker_count": [2, 0, 4, ...],
        "blocked_by_load": [0, 1, 0, ...],
        "blocked_by_memory": [0, 0, 1, ...]
    },
    "business_metrics": {
        "sats_per_hour": [1500, 2200, ...],
        "tasks_completed": [3, 1, 0, ...],
        "crash_count": [0, 2, 1, ...],
        "cost_per_task_sats": [500, 2200, ...]
    },
    "violations_heatmap": {
        "load_violations": [0, 5, 0, ...],
        "memory_violations": [0, 0, 3, ...],
        "rate_limit_violations": [1, 8, 2, ...]
    }
}
```

---

## Phase 4: Kalman Filter Integration (Priority: MEDIUM)

### 4.1 **Multi-Resource Kalman Extension**
**File**: `~/.hermes/bot/burn_predictor.py`
**Enhancement**: Add system resources to state vector

```python
class MultiResourceKalmanPredictor:
    def __init__(self):
        # State: [tokens, cpu_load, memory_pct, worker_count]
        self.state = np.array([1000.0, 0.5, 50.0, 2.0])
        
        # Extended measurement noise matrix
        self.R = np.diag([
            1000000.0,    # Token variance (high)
            0.1,         # CPU load variance (low)
            1.0,         # Memory variance (medium)
            0.01         # Worker count variance (low)
        ])
        
        # Process noise for resource dynamics
        self.Q = np.diag([
            100000.0,    # Token burn rate change
            0.01,        # CPU load change rate
            0.1,         # Memory change rate  
            0.001        # Worker count change rate
        ])
    
    def predict_resource_exhaustion(self, hours_ahead):
        """Predict when any resource will hit critical threshold"""
        predictions = []
        
        for h in range(1, hours_ahead + 1):
            state_h = self.predict_step(h)
            
            # Check each resource constraint
            alerts = []
            if state_h[1] > 6.0:  # CPU load > 6.0
                alerts.append(f"CPU load critical: {state_h[1]:.1f}")
            if state_h[2] > 85.0:  # Memory > 85%
                alerts.append(f"Memory critical: {state_h[2]:.1f}%")
            if state_h[3] > 10:   # Workers > 10
                alerts.append(f"Worker count critical: {int(state_h[3])}")
            
            predictions.append({
                'hour': h,
                'state': state_h.copy(),
                'alerts': alerts
            })
        
        return predictions
```

### 4.2 **Resource-Aware Dispatch Integration**
**File**: `~/.hermes/profiles/manager/scripts/safe-fips-dispatch-gate.sh`
**Integration**: Use Kalman predictions for dispatch decisions

```bash
# Enhanced dispatch logic using Kalman predictions
kalman_predictions=$(python3 -c "
from burn_predictor import MultiResourceKalmanPredictor
k = MultiResourceKalmanPredictor()
predictions = k.predict_resource_exhaustion(2)  # 2 hours ahead
print('safe:' if not predictions[0]['alerts'] else 'unsafe:')
")

if [[ $kalman_predictions == *"safe:"* ]]; then
    echo "Kalman predicts safe resource levels - dispatching workers"
    spawn_workers_safely
else
    echo "Kalman predicts resource exhaustion - deferring dispatch"
    defer_non_critical_tasks
fi
```

---

## Implementation Timeline

### **Week 1 (Emergency Stabilization)**
- [x] **Day 1**: Implement Phase 1 crash fixes (rate limiting, hysteresis, graceful restarts)
- [ ] **Day 2-3**: Deploy system resource monitoring scripts
- [ ] **Day 4-5**: Collect baseline data and verify crash reduction

### **Week 2 (Dashboard Enhancement)**
- [ ] **Day 6-7**: Extend build_v3.py for system resource charts
- [ ] **Day 8-9**: Implement resource violations heatmap
- [ ] **Day 10**: Deploy enhanced dashboard and verify visualization quality

### **Week 3 (Kalman Integration)**
- [ ] **Day 11-12**: Extend KalmanPredictor for multi-resource predictions
- [ ] **Day 13**: Integrate Kalman predictions with dispatch logic
- [ ] **Day 14**: Test and validate prediction accuracy

### **Week 4 (Optimization & Tuning)**
- [ ] **Day 15-16**: Fine-tune thresholds and visualization parameters
- [ ] **Day 17-18**: Implement alerting and notification system
- [ ] **Day 19-20**: Performance optimization and documentation

---

## Success Metrics

### **Short-term (1 week)**
- [ ] **Crash Reduction**: 75% reduction in unexpected worker crashes
- [ ] **Stability**: No more emergency scaling oscillations (0→4→0 swings)
- [ ] **Data Collection**: All system resources tracked every 5 minutes

### **Medium-term (2 weeks)**  
- [ ] **Visualization**: Dashboard shows CPU, memory, worker count with proper axis labels
- [ ] **Correlation**: Resource violations heatmap showing constraint patterns
- [ ] **Business Metrics**: SATs/task cost tracking with crash correlation

### **Long-term (1 month)**
- [ ] **Prediction**: Kalman filters accurately predict resource exhaustion 30+ min ahead
- [ ] **Self-Healing**: System automatically prevents crashes using predictions
- [ ] **Cost Optimization**: 50% reduction in SATs/task through resource-aware dispatch

---

## Files to Create/Modify

### **New Files**
1. `system-resource-collector.sh` - Comprehensive metrics collection
2. `crash-monitor.sh` - Crash detection and analysis  
3. `graceful-restart-proxy.sh` - Safe proxy restart procedure
4. `multi-resource-kalman.py` - Extended Kalman predictor class

### **Modified Files**
1. `safe-fips-dispatch-gate.sh` - Add rate limiting, hysteresis, Kalman integration
2. `build_v3.py` - Add system resource charts and business metrics
3. `burn_predictor.py` - Extend for multi-resource prediction
4. `~/.hermes/profiles/worker-admin/config.yaml` - Adjust scaling thresholds

### **Database Schema Updates**
```sql
ALTER TABLE api_calls ADD COLUMN system_state TEXT;
CREATE TABLE resource_metrics (ts INTEGER, cpu_load_1m REAL, ...);
CREATE TABLE crash_events (ts INTEGER, crash_count INTEGER, ...);
```

---

## Critical Success Factors

1. **Logarithmic Scaling**: All high-dynamic-range plots must use log scale (CPU 0.5→38, workers 0→49)
2. **Proper Axis Labels**: Every axis must have clear labels with units (%, MB, processes/core)
3. **Real-time Data**: 5-minute collection intervals for timely decision making
4. **Gradual Implementation**: Deploy crash fixes first, then monitoring, then prediction
5. **Documentation**: All changes committed to ngit with clear commit messages

---

**Next Immediate Action**: Implement Phase 1 crash fixes to stabilize the system before proceeding with dashboard enhancements. The crash patterns are actively blocking work and require immediate attention.