# Merchant Module — Session Bootstrap & Initial Planning

## Session Context
**Date**: 2026-07-08  
**Group**: merchant-module (Signal)  
**Session ID**: This session needs session ID assignment  
**Participants**: c03r4d0r number 3, Hermes Agent

## Problem Statement
Hermes and TollGate need a **Merchant Module** for making optimal business decisions across multiple resource constraints. Current issues:
- PPQ.AI credit burn causing financial loss
- System resource constraints (CPU, memory) blocking work and causing crashes
- Lost context when sessions crash, requiring manual recovery
- Need for Kalman filter-based decision optimization

## Decisions Made in This Session

### 1. Session Scope Definition ✅
**What we're focusing on**:
- Multi-resource optimization (API costs, CPU, memory, network)
- Business decision engine for economic actors
- Initial dogfooding on Hermes AI and TollGate
- Kalman filter extensions for system resources

**What we're excluding**:
- General TollGate protocol implementation (→ tollgate-android group)
- Hermes infrastructure without business context (→ hermes-orchestration group)
- FIPS networking details (→ dedicated FIPS group)
- PPQ emergency fixes (→ ppq-bleed-detection-and-fix skill, already resolved)

### 2. Kanban Board Setup ✅
**Action**: Created dedicated `merchant-module` board  
**Command**: `hermes kanban boards create merchant-module`  
**DB Path**: `/home/c03rad0r/.hermes/kanban/boards/merchant-module/kanban.db`

### 3. Master Plan Creation ✅
**File**: `~/plans/merchant-module-master-plan.md`  
**Status**: Committed (bbab041) and pushed to ngit  
**Content**: 4-phase implementation roadmap covering:
- Phase 1: Core Kalman extension for system resources
- Phase 2: Hermes integration for reliable dispatch
- Phase 3: TollGate upstream provider optimization
- Phase 4: Enhanced dashboard with business metrics

### 4. Architecture Decision ✅
**Approach**: Dogfood-first with specific use cases
- **Primary**: Hermes AI resource optimization (prevent crashes, optimize API costs)
- **Secondary**: TollGate upstream provider business decisions
- **Pattern**: Common Kalman engine, domain-specific implementations

### 5. Documentation Protocol ✅
**Requirement**: All decisions and progress must be documented in markdown files
**Why**: Sessions crash, context lost, other LLM sessions need to resume work
**Pattern**: 
1. Create session summary documents like this one
2. Commit and push to ngit before session ends
3. Other sessions can read ngit to understand state
4. Maintain continuous version control

## Current Progress

### Completed ✅
- [x] Session scope definition and exclusions
- [x] Merchant module kanban board created
- [x] Master plan written and committed (bbab041)
- [x] Bootstrap message delivered to group
- [x] Documentation protocol established

### Next Steps (Ready for Next Session)
- [ ] **Phase 1 Priority**: Extend existing KalmanPredictor (`~/.hermes/bot/burn_predictor.py`) for system resources
- [ ] **System Resource Collector**: Extend ContextVM to collect CPU/memory metrics
- [ ] **Multi-Constraint Decision Logic**: Implement resource safety thresholds and fallbacks
- [ ] **Architecture Decision**: Standalone `merchant-module` repo vs extending existing infrastructure

### Files Created/Modified
1. **`~/plans/merchant-module-master-plan.md`** - Master plan with 4 phases
2. **`~/plans/merchant-module-session-bootstrap.md`** - This session summary (NOT YET COMMITTED)

### Repositories Involved
- **`~/plans/`** - Version-controlled plans (ngit remote)
- **`~/.hermes/kanban/boards/merchant-module/`** - Kanban board (will be version-controlled)
- **Future**: `merchant-module` repo (standalone or submodule)

### Key References for Resuming Work
1. **PPQ Bleed Context**: See `ppq-bleed-detection-and-fix` skill for financial leak fixes
2. **Kalman Infrastructure**: `~/.hermes/bot/burn_predictor.py`, `kalman_health.py`
3. **Current System Issues**: Cron logs showing LOAD_TOO_HIGH, MEM_TOO_LOW blocking work
4. **TollGate Context**: `~/plans/tollgate-fips-master-plan.md` for provider decisions

## Technical Debt & Known Issues

### 1. Session Recovery
**Problem**: This session doesn't have a session ID yet - need to track for future reference
**Solution**: Next sessions should capture session ID for better continuity

### 2. Dashboard Gaps
**Problem**: Current nsite dashboard only tracks API costs, missing CPU/memory
**Location**: `~/nsites/kalman-data/scripts/build_v3.py`
**Needs**: Log scale pricing, system resource plots, constraint violations heatmap

### 3. Dispatch Gate Optimization
**Current**: Safe FIPS Dispatch Gate uses single constraints
**Needed**: Multi-resource decision logic from merchant module
**File**: `~/.hermes/profiles/manager/scripts/safe-fips-dispatch-gate.sh`

## Open Questions for Next Session
1. **Architecture**: Should we create a standalone `merchant-module` repo or extend existing infrastructure?
2. **Priority Order**: Focus first on Hermes crash prevention or TollGate business optimization?
3. **Data Model**: What specific metrics needed for TollGate provider decisions?
4. **Integration**: How to coordinate decisions between Hermes and TollGate modules?

## How to Resume This Work
1. **Read This Document**: All decisions, progress, and next steps documented here
2. **Check Master Plan**: `~/plans/merchant-module-master-plan.md` for detailed roadmap
3. **Review Kanban Board**: Switch to merchant-module board for current tasks
4. **Examine Existing Code**: Start with `~/.hermes/bot/burn_predictor.py` for Kalman extension
5. **Check System State**: Look at cron logs for current resource constraint issues

## Session Notes for Other LLMs
If you're a new session taking over this work:
1. **Start Here**: Read this document and the master plan
2. **Current State**: Phase 1 ready - extending Kalman for system resources
3. **Urgency**: Crash cycles causing real work blockage - prioritize stability
4. **Pattern**: Document everything, commit often, push to ngit
5. **Contact**: c03r4d0r number 3 is the decision maker for this module

---

**Session End Checklist**:
- [x] All decisions documented
- [x] Next steps clearly defined  
- [x] File locations and references recorded
- [x] Open questions noted
- [ ] Commit this session summary to ngit
- [ ] Verify merchant-module board is ready for next session