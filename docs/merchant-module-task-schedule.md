# Merchant Module Task Schedule & Board Management

## Board Status: ✅ Ready to Execute

**Current Board**: merchant-module (successfully renamed from SX-1280)  
**Emergency Tasks**: 3 tasks with Priority 9 (execute immediately)  
**Total Tasks**: 19 comprehensive tasks across all phases

---

## 🚨 EMERGENCY TASKS (Priority 9 - Execute Today)

### **Task 1: t_de8273dc - Fix API Rate Limit Cascading Failures**
- **Status**: Ready 
- **Assignee**: Available for immediate assignment
- **Goal**: Implement staggered worker dispatch with rate limiting and backoff to prevent 429 storms
- **Impact**: Prevents $1.43+ cost bleeds and worker crashes

**Immediate Actions Required**:
1. Reduce MAX_WORKERS_CONCURRENT to 2 (from current unlimited)
2. Add $API_RATE_LIMIT_BUFFER (5s delay between worker spawns)
3. Implement WORKER_RETRY_DELAY (30s before respawning crashed workers)
4. Add proxy health check before spawning workers
5. **Files**: `~/.hermes/profiles/manager/scripts/safe-fips-dispatch-gate.sh`

### **Task 2: t_3384551f - Fix Emergency Scaling Oscillations**
- **Status**: Ready
- **Goal**: Implement hysteresis to prevent 0→4→0 worker swings
- **Root Issue**: 56% memory threshold triggers emergency scaling every 10 seconds
- **Solution**: Emergency stops at 80%, resumes at 65%, gradual ±1 adjustments

### **Task 3: t_db96c67f - Implement Graceful Proxy Restarts**
- **Status**: Ready  
- **Goal**: Prevent connection reset crashes during service restarts
- **Solution**: Implement `graceful-restart-proxy.sh` with worker lifecycle management

---

## 📊 PHASE 2 TASKS (Priority 8 - This Week)

### **Task 4: t_403ef6eb - Create System Resource Monitoring Scripts**
- **Goal**: Comprehensive metrics collection every 5 minutes
- **Components**: 
  - `system-resource-collector.sh` (CPU, memory, disk, network)
  - `crash-monitor.sh` (crash detection and analysis)
  - Database schema: `resource_metrics` and `crash_events` tables

### **Task 5: t_fa4fd955 - Extend Dashboard with System Resource Charts**
- **Goal**: Add system resource visualization with log scaling and proper axis labels
- **Charts**:
  - System Resource Overview (log scale CPU, memory, workers)
  - Resource Violations Heatmap (crash correlation)
  - Enhanced Business Metrics (SATs/task cost)

---

## 🧠 PHASE 3 TASKS (Priority 7 - Next 2 Weeks)

### **Task 6: t_4c6bc170 - Extend KalmanPredictor for Multi-Resource Management**
- **Goal**: Multi-dimensional Kalman filter for tokens, CPU, memory, workers
- **State Vector**: `[tokens, cpu_load, memory_pct, worker_count]`
- **Output**: Resource exhaustion predictions 30+ minutes ahead

### **Task 7: t_de63f6dc - Implement Smart Key Selection with Task Quality Inputs**
- **Goal**: Intelligent key and model selection based on task requirements
- **Task Inputs**:
  - Estimated duration
  - Quality LLM required
  - Urgency level
  - Cost optimization preference
- **Decision Logic**: Balance cost, quality, response time

---

## 🔍 PHASE 4 TASKS (Priority 6 - Future Enhancement)

### **Task 8: t_79ef3cbb - Add LLM Benchmarking Integration**
- **Goal**: Use benchmark data (HELM, MMLU, GSM8K, HumanEval) for model selection
- **Integration**: Benchmark metrics as input to Kalman filters
- **Output**: Better LLM selection decisions based on performance data

### **Task 9: t_bbbfc10e - Implement Multi-Provider Price Comparison**
- **Goal**: Price tracking across ppq.ai, openrouter, and other providers
- **Components**:
  - Real-time price monitoring
  - Same-model price comparison
  - Cost optimization recommendations

---

## 🏗️ EXISTING BOARD TASKS

### **Already Running (3 tasks)**
- `t_32d883d8` - 5-node mesh test (worker-base)
- `t_24467797` - Crash detection script (worker-tollgate) 
- `t_1ef2dcf3` - Intelligent key selection (worker-admin)

### **Blocked Tasks (11 tasks)**
- Various mesh, deployment, and integration tasks
- Will be unblocked as emergency fixes complete

### **Todo Tasks (6 tasks)**
- Phase 2-4 implementation tasks ready to start

---

## 📋 TASK EXECUTION PLAN

### **Day 1 (Today) - Emergency Phase**
```
09:00 - Assign t_de8273dc (API Rate Limit Fix) - CRITICAL
10:00 - Assign t_3384551f (Scaling Oscillations) - HIGH  
11:00 - Assign t_db96c67f (Graceful Restarts) - HIGH
14:00 - Monitor crash reduction metrics
16:00 - Adjust configurations if needed
```

### **Day 2-3 - Stabilization Phase**
```
09:00 - Assign t_403ef6eb (Resource Monitoring)
10:00 - Start t_fa4fd955 (Dashboard Charts) 
11:00 - Begin data collection
16:00 - Verify 75% crash reduction achieved
```

### **Day 4-7 - Enhancement Phase**
```
09:00 - Assign t_4c6bc170 (Kalman Extension)
10:00 - Assign t_de63f6dc (Smart Key Selection)
14:00 - Assign Phase 4 tasks (benchmarking, pricing)
```

---

## 🎯 SUCCESS METRICS

### **Emergency Phase (24 hours)**
- ✅ 75% reduction in 429 errors
- ✅ No more emergency scaling oscillations (0→4→0 swings stopped)
- ✅ Graceful proxy restarts without worker crashes

### **Stabilization Phase (1 week)**
- ✅ System resource monitoring active (5-minute intervals)
- ✅ Dashboard shows CPU/memory with log scaling and proper labels
- ✅ Resource violations heatmap operational

### **Enhancement Phase (2 weeks)**
- ✅ Multi-resource Kalman predictions working
- ✅ Smart key selection using task quality inputs
- ✅ LLM benchmarking data integrated

---

## 🔄 BOARD MANAGEMENT

### **Renaming Complete**
- ✅ Board renamed from `SX-1280` to `merchant-module`
- ✅ No confusion for future LLM sessions about SX-1280 usage
- ✅ All tasks properly scoped for merchant module objectives

### **Task Assignment Strategy**
- **Priority 9**: Assign immediately to available workers
- **Priority 8**: Assign after emergency tasks stabilize
- **Priority 7**: Assign as worker capacity becomes available
- **Priority 6**: Queue for future enhancement work

### **Blocking Resolution**
- **Current blocked tasks**: 11 tasks waiting for emergency fixes
- **Unblocking timeline**: As emergency tasks complete, blocked tasks will naturally unblock
- **Dependency chain**: Emergency → Stabilization → Enhancement

---

## 📞 READY FOR EXECUTION

The merchant-module kanban board is fully configured and ready:

- **3 emergency tasks** (Priority 9) - assign and execute immediately
- **2 stabilization tasks** (Priority 8) - start tomorrow
- **2 enhancement tasks** (Priority 7) - queue for next week
- **2 future tasks** (Priority 6) - plan for future iterations

**Next Step**: Assign the emergency tasks to available workers and begin execution immediately. The system stabilization will prevent further $1.43+ cost bleeds and worker crashes.