# Merchant Module — Enhanced Comprehensive Plan with Global Task Tracking

## Executive Summary

This enhanced comprehensive plan includes all previously identified components plus **Global Task Tracking** across all kanban boards. The dashboard will now provide visibility into system-wide task distribution, bottlenecks, and throughput patterns with both real-time metrics and historical plots.

---

## 📊 NEW FEATURE: Global Task Tracking Dashboard

### **Real-Time Task Metrics Panel**
Add to the dashboard (right side or top section) a compact summary showing:

```
┌─────────────────────────────────────────────────────────────┐
│ 🔴 GLOBAL TASK STATUS ACROSS ALL BOARDS                    │
├───────────────┬───────────────┬───────────────┬───────────────┤
│ WAITING      │ RUNNING      │ COMPLETED     │ BLOCKED       │
├───────────────┼───────────────┼───────────────┼───────────────┤
│ 42 tasks     │ 18 tasks     │ 156 tasks     │ 23 tasks      │
│ across 93    │ across 93    │ across 93     │ across 93     │
│ boards       │ boards       │ boards        │ boards        │
├───────────────┴───────────────┴───────────────┴───────────────┤
│ 🟢 THROUGHPUT: 23 tasks completed in last 24 hours           │
│ 🟡 BOTTLENECK: FIPS board (67% blocked tasks)               │
│ 🔴 CRITICAL: 3 emergency tasks waiting assignment           │
└─────────────────────────────────────────────────────────────┘
```

### **Historical Task Flow Plot (NEW)**
Add Chart 4 to the dashboard showing task distribution over time:

```javascript
// Chart 4: Global Task Flow Analysis
const taskFlowChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: hourly_timestamps,  // Last 7 days, hourly
        datasets: [
            {
                label: 'Waiting Tasks',
                data: waiting_count_history,
                borderColor: 'rgb(255, 193, 7)',   // Amber
                yAxisID: 'y-axis-count',
                fill: false
            },
            {
                label: 'Running Tasks', 
                data: running_count_history,
                borderColor: 'rgb(52, 211, 153)', // Green
                yAxisID: 'y-axis-count',
                fill: false
            },
            {
                label: 'Completed Tasks',
                data: completed_count_history,
                borderColor: 'rgb(59, 130, 246)', // Blue  
                yAxisID: 'y-axis-completed',
                fill: false
            },
            {
                label: 'Blocked Tasks',
                data: blocked_count_history,
                borderColor: 'rgb(239, 68, 68)',   // Red
                yAxisID: 'y-axis-count',
                fill: true
            }
        ]
    },
    options: {
        scales: {
            'y-axis-count': {
                type: 'logarithmic',  // Log scale for high dynamic range
                position: 'left',
                title: { display: true, text: 'Task Count (waiting/running/blocked, log scale)' },
                min: 1,
                ticks: { callback: function(value) { return value.toLocaleString(); } }
            },
            'y-axis-completed': {
                type: 'linear',
                position: 'right', 
                title: { display: true, text: 'Completed Tasks' },
                min: 0,
                ticks: { stepSize: 5 }
            }
        },
        plugins: {
            title: {
                display: true,
                text: 'Global Task Flow Analysis - All Boards (Last 7 Days)'
            },
            tooltip: {
                callbacks: {
                    afterLabel: function(context) {
                        const hour_data = getHourlyBoardBreakdown(context.parsed.x);
                        return `Top boards: ${hour_data.top_boards.join(', ')}`;
                    }
                }
            }
        }
    }
});
```

---

## 🚰 DATA COLLECTION FOR GLOBAL TASK TRACKING

### **New Script: `global-task-monitor.sh`**
Create in `~/.hermes/profiles/manager/scripts/`:

```bash
#!/bin/bash
# global-task-monitor.sh - Collect task metrics across all kanban boards

GLOBAL_TASK_DB="/home/c03rad0r/.hermes/bot/global_task_metrics.db"

init_global_task_db() {
    sqlite3 "$GLOBAL_TASK_DB" << EOF
CREATE TABLE IF NOT EXISTS global_task_snapshots (
    ts INTEGER PRIMARY KEY,
    total_boards INTEGER,
    waiting_count INTEGER,
    running_count INTEGER, 
    completed_count INTEGER,
    blocked_count INTEGER,
    top_board_waiting TEXT,
    top_board_blocked TEXT,
    emergency_waiting INTEGER,
    throughput_24h INTEGER
);

CREATE TABLE IF NOT EXISTS board_task_metrics (
    ts INTEGER,
    board_name TEXT,
    waiting_count INTEGER,
    running_count INTEGER,
    completed_count INTEGER,
    blocked_count INTEGER,
    PRIMARY KEY (ts, board_name)
);
EOF
}

collect_global_task_metrics() {
    local ts=$(date +%s)
    
    # Get list of all boards
    local boards=$(hermes kanban boards list --quiet | awk '/● / {print $2}')
    local total_boards=$(echo "$boards" | wc -l)
    
    # Initialize counters
    local total_waiting=0 total_running=0 total_completed=0 total_blocked=0
    local max_waiting_board="" max_waiting_count=0
    local max_blocked_board="" max_blocked_count=0
    local emergency_waiting=0
    
    # Collect metrics per board
    local board_metrics=""
    while read -r board; do
        if [[ -n "$board" ]]; then
            local metrics=$(hermes kanban --board "$board" list --format json 2>/dev/null)
            if [[ -n "$metrics" ]]; then
                local waiting=$(echo "$metrics" | jq '.waiting | length' 2>/dev/null || echo 0)
                local running=$(echo "$metrics" | jq '.running | length' 2>/dev/null || echo 0)
                local completed=$(echo "$metrics" | jq '.done | length' 2>/dev/null || echo 0)
                local blocked=$(echo "$metrics" | jq '.blocked | length' 2>/dev/null || echo 0)
                
                # Update totals
                total_waiting=$((total_waiting + waiting))
                total_running=$((total_running + running))
                total_completed=$((total_completed + completed))
                total_blocked=$((total_blocked + blocked))
                
                # Track worst boards
                if [[ $waiting -gt $max_waiting_count ]]; then
                    max_waiting_count=$waiting
                    max_waiting_board="$board"
                fi
                if [[ $blocked -gt $max_blocked_count ]]; then
                    max_blocked_count=$blocked
                    max_blocked_board="$board"
                fi
                
                # Count emergency tasks (priority 9 in merchant-module)
                if [[ "$board" == "merchant-module" ]]; then
                    emergency_waiting=$(echo "$metrics" | jq '.ready[] | select(.priority == 9) | length' 2>/dev/null || echo 0)
                fi
                
                # Store board metrics
                board_metrics+="$ts|$board|$waiting|$running|$completed|$blocked\n"
            fi
        fi
    done <<< "$boards"
    
    # Calculate 24h throughput
    local throughput_24h=$(sqlite3 "$GLOBAL_TASK_DB" "
        SELECT COUNT(*) FROM board_task_metrics 
        WHERE ts > $(($ts - 86400)) AND board_name != 'global';
    " 2>/dev/null || echo 0)
    
    # Store global snapshot
    sqlite3 "$GLOBAL_TASK_DB" << EOF
INSERT INTO global_task_snapshots VALUES (
    $ts, $total_boards, $total_waiting, $total_running, 
    $total_completed, $total_blocked, '$max_waiting_board',
    '$max_blocked_board', $emergency_waiting, $throughput_24h
);
EOF
    
    # Store board metrics
    echo -e "$board_metrics" | while IFS='|' read -r ts board w r c b; do
        sqlite3 "$GLOBAL_TASK_DB" << EOF
INSERT OR REPLACE INTO board_task_metrics VALUES 
    ($ts, '$board', $w, $r, $c, $b);
EOF
    done
    
    echo "✅ Global task metrics collected: $total_boards boards, $total_waiting waiting, $emergency_waiting emergency"
}

# Run collection
collect_global_task_metrics
```

### **Cron Job Setup**
Add to `crontab -e`:
```bash
# Collect global task metrics every 5 minutes
*/5 * * * * /home/c03rad0r/.hermes/profiles/manager/scripts/global-task-monitor.sh
```

---

## 🔧 DASHBOARD ENHANCEMENTS

### **Update `build_v3.py` for Global Task Tracking**
Add to data generation section:

```python
def generate_global_task_data():
    """Generate global task metrics for dashboard."""
    db_path = Path.home() / ".hermes" / "bot" / "global_task_metrics.db"
    
    if not db_path.exists():
        return {"error": "Global task database not found"}
    
    db = sqlite3.connect(str(db_path))
    
    # Global snapshots (last 7 days, hourly)
    snapshots = db.execute("""
        SELECT ts, total_boards, waiting_count, running_count, 
               completed_count, blocked_count, top_board_waiting,
               top_board_blocked, emergency_waiting, throughput_24h
        FROM global_task_snapshots 
        WHERE ts > (SELECT MAX(ts) - 604800 FROM global_task_snapshots)
        ORDER BY ts ASC
    """).fetchall()
    
    # Board breakdown (current state)
    boards = db.execute("""
        SELECT board_name, waiting_count, running_count,
               completed_count, blocked_count
        FROM board_task_metrics 
        WHERE ts = (SELECT MAX(ts) FROM board_task_metrics)
        ORDER BY waiting_count DESC
    """).fetchall()
    
    # Calculate hourly aggregates for plotting
    hourly_data = db.execute("""
        SELECT 
            CAST(ts / 3600 AS INTEGER) * 3600 as hour_ts,
            SUM(waiting_count) as waiting,
            SUM(running_count) as running, 
            SUM(completed_count) as completed,
            SUM(blocked_count) as blocked
        FROM board_task_metrics
        WHERE ts > (SELECT MAX(ts) - 604800 FROM board_task_metrics)
        GROUP BY hour_ts
        ORDER BY hour_ts ASC
    """).fetchall()
    
    db.close()
    
    return {
        "global_snapshots": snapshots,
        "current_boards": boards,
        "hourly_flow": hourly_data
    }

# Add to main data generation function
def generate_data_json():
    # ... existing code ...
    
    # Add global task data
    global_task_data = generate_global_task_data()
    data.update(global_task_data)
    
    # ... rest of existing code ...
```

### **Add Global Task Summary to Dashboard HTML**
Add after the KPI cards (around line 407):

```html
<!-- Global Task Status Panel -->
<div class="card" style="grid-column: 1 / -1;">
    <h3>🌍 Global Task Status (All Boards)</h3>
    <div class="grid">
        <div>
            <div class="v" id="globalWaiting" style="color:#fbbf24;">...</div>
            <div class="note">Waiting Tasks</div>
        </div>
        <div>
            <div class="v" id="globalRunning" style="color:#34d399;">...</div>
            <div class="note">Running Tasks</div>
        </div>
        <div>
            <div class="v" id="globalCompleted" style="color:#3b82f6;">...</div>
            <div class="note">Completed Tasks</div>
        </div>
        <div>
            <div class="v" id="globalBlocked" style="color:#ef4444;">...</div>
            <div class="note">Blocked Tasks</div>
        </div>
        <div>
            <div class="v" id="throughput24h" style="color:#8b5cf6;">...</div>
            <div class="note">24h Throughput</div>
        </div>
        <div>
            <div class="v" id="emergencyCount" style="color:#dc2626;">...</div>
            <div class="note">Emergency Tasks</div>
        </div>
    </div>
    <div class="note" style="margin-top:8px; font-size:0.8em;" id="globalSummary">...</div>
</div>
```

### **Add Global Task Flow Chart**
Add after existing charts (around line 427):

```html
<h2>Global Task Flow Analysis <span class="note"> All boards - last 7 days (log scale)</span></h2>
<div class="chart" id="c4"></div>
```

---

## 📈 ENHANCED SUCCESS METRICS

### **Global Task Tracking Metrics (NEW)**

#### **Real-Time KPIs**
- **Task Distribution**: Balance across all boards (target: no board > 30% of total tasks)
- **Emergency Response**: Time from emergency task creation to assignment (target: < 5 minutes)
- **Bottleneck Identification**: Top blocked boards highlighted (target: investigate boards with > 50% blocked rate)

#### **Historical Trends**
- **Throughput Rate**: Tasks completed per 24 hours (target: increasing trend)
- **Cycle Time**: Average time from task creation to completion (target: decreasing trend)
- **Blockage Rate**: Percentage of tasks that become blocked (target: < 20%)

#### **System Health Indicators**
- **Workload Balance**: Standard deviation of task distribution across boards
- **Emergency Load**: Number of priority 9 tasks in system (target: < 3 at any time)
- **Throughput Efficiency**: Completed tasks vs running tasks ratio (target: > 2:1)

---

## 🎯 UPDATED IMPLEMENTATION TIMELINE

### **Week 1: Emergency + Global Tracking**
- **Day 1**: Execute 3 emergency tasks (Priority 9)
- **Day 2**: Implement `global-task-monitor.sh` script
- **Day 3**: Add global task panel to dashboard
- **Day 4**: Deploy task flow historical plot
- **Day 5**: Tune collection intervals and visualization

### **Week 2: Stabilization + Analytics**
- **Day 6-7**: System resource monitoring and dashboard charts
- **Day 8-9**: Global task analytics and bottleneck identification
- **Day 10**: Optimize task distribution and emergency response

### **Week 3-4: Enhancement + Optimization**
- **Day 11-14**: Multi-resource Kalman and smart key selection
- **Day 15-18**: LLM benchmarking and multi-provider pricing
- **Day 19-20**: Global task optimization and predictive scaling

---

## 🔄 INTEGRATION WITH EXISTING PLANS

### **Merchant Module Integration**
The global task tracking integrates with all previously planned components:

1. **Emergency Response**: Identifies and prioritizes emergency tasks across all boards
2. **Resource Optimization**: Uses task distribution data to balance workload
3. **Smart Key Selection**: Considers task importance when making API decisions
4. **Cost Optimization**: Tracks task completion costs across all boards

### **Dashboard Enhancement**
The enhanced dashboard now provides:

1. **System Visibility**: Real-time task status across all boards
2. **Historical Analysis**: Task flow trends and bottleneck identification  
3. **Performance Metrics**: Throughput, cycle time, and efficiency tracking
4. **Emergency Awareness**: Immediate visibility of critical tasks

### **Decision Support**
Global task data enables better business decisions:

1. **Resource Allocation**: Balance workload across available workers
2. **Priority Management**: Ensure critical tasks get immediate attention
3. **Capacity Planning**: Predict future resource needs based on trends
4. **Performance Optimization**: Identify and address bottlenecks

---

## 📊 SUCCESS METRICS REVISION

### **Enhanced KPIs (Including Global Tracking)**

#### **Emergency Response (24 hours)**
- ✅ 75% reduction in API crashes and cost bleeds
- ✅ < 5 minute response time for emergency tasks
- ✅ Global task visibility operational

#### **System Monitoring (1 week)**  
- ✅ System resource tracking active (5-minute intervals)
- ✅ Global task panel operational with real-time updates
- ✅ Task flow historical plot showing 7-day trends

#### **Predictive Management (2 weeks)**
- ✅ Multi-resource Kalman predictions working
- ✅ Smart key selection using task importance data
- ✅ Global throughput optimization active

#### **System Optimization (1 month)**
- ✅ 90% crash reduction through predictive prevention
- ✅ Task distribution balance across all boards
- ✅ Cost optimization using global task analytics

---

**IMPLEMENTATION READY**: All emergency tasks scheduled, global task tracking architecture defined, and dashboard enhancements planned. The system will now provide complete visibility across all kanban boards with both real-time metrics and historical analysis.

*Last Updated: 2026-07-08 (Enhanced with Global Task Tracking)*