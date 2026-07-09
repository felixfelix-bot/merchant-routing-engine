# Merchant Module — Distributed Resource Integration Plan

## Executive Summary

This plan addresses how firecracker VMs and DQ05 mini PC fit into the merchant module. Based on investigation, these represent distributed computing resources that extend the merchant module's multi-resource optimization beyond a single machine to a distributed infrastructure environment.

## Current Understanding

### What We Found
- **Firecracker VMs**: Lightweight virtual machines, likely used for isolated workloads
- **DQ05 Mini PC**: Intel NUC-like mini PC, probably serving as edge computing node
- **Infrastructure Context**: Reference to VPS2 in sovereign-shops plan suggests distributed architecture
- **No Direct Configs**: No immediate SSH access or configuration files found

### Inferred Capabilities
```yaml
firecracker_vms:
  type: "Lightweight virtual machines"
  use_case: "Isolated workloads, security boundaries, resource partitioning"
  benefits: ["Resource isolation", "Security", "Scalability", "Cost efficiency"]

dq05_mini_pc:
  type: "Edge computing device"
  use_case: "Local processing, network optimization, sensor integration"
  benefits: ["Low latency", "Distributed processing", "Offline capability", "Reduced bandwidth"]
```

## Integration into Merchant Module

### Phase 1: Distributed Resource Monitoring (Week 2)

#### 1.1 **Extended Resource Collection Script**
**Goal**: Monitor distributed resources alongside local system

**Enhanced System Resource Collector**:
```bash
# Extended to collect distributed metrics
collect_distributed_metrics() {
    # Local metrics (existing)
    collect_local_resources
    
    # Firecracker VM metrics
    for vm in firecracker-vm1 firecracker-vm2 firecracker-vm3; do
        if ssh_alive $vm; then
            ssh $vm "cat /proc/loadavg | awk '{print \$1,\$2,\$3}'" > /tmp/${vm}_load
            ssh $vm "free -m | awk '/Mem:/ {print \$2,\$7}'" > /tmp/${vm}_mem
            ssh $vm "cat /proc/net/dev | grep ens33 | awk '{print \$2,\$10}'" > /tmp/${vm}_net
        fi
    done
    
    # DQ05 mini PC metrics  
    if ssh_alive dq05; then
        ssh dq05 "cat /proc/loadavg | awk '{print \$1,\$2,\$3}'" > /tmp/dq05_load
        ssh dq05 "free -m | awk '/Mem:/ {print \$2,\$7}'" > /tmp/dq05_mem
        ssh dq05 "cat /proc/net/dev | grep eth0 | awk '{print \$2,\$10}'" > /tmp/dq05_net
    fi
}
```

#### 1.2 **Database Schema Extension**
**Goal**: Track distributed machine resources

```sql
ALTER TABLE resource_metrics ADD COLUMN machine_name TEXT DEFAULT 'main';
ALTER TABLE resource_metrics ADD COLUMN machine_type TEXT DEFAULT 'physical';

-- Distributed machine inventory
CREATE TABLE distributed_machines (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    type TEXT CHECK (type IN ('firecracker-vm', 'mini-pc', 'vps', 'physical')),
    hostname TEXT,
    ssh_host TEXT,
    ssh_port INTEGER DEFAULT 22,
    capabilities TEXT,  -- JSON array: ["gpu", "nvme", "low-latency"]
    active BOOLEAN DEFAULT true,
    last_seen_ts INTEGER,
    location TEXT,     -- "local", "edge", "cloud", "datacenter"
    role TEXT          -- "compute", "storage", "network", "edge"
);
```

### Phase 2: Distributed Resource Optimization (Week 3)

#### 2.1 **Multi-Machine Kalman Filter**
**Goal**: Extend Kalman predictions to distributed resources

**Enhanced Kalman State Vector**:
```python
class DistributedResourceKalmanPredictor:
    def __init__(self):
        # State includes multiple machines
        self.state = np.array([
            # Main machine
            1000.0, 0.5, 50.0, 2.0,
            # Firecracker VM 1
            500.0, 0.3, 30.0, 1.0,
            # Firecracker VM 2
            500.0, 0.2, 35.0, 1.0,
            # DQ05 Mini PC
            800.0, 0.1, 20.0, 0.5
        ])
        
        # Extended measurement noise matrix
        self.R = np.diag([
            # Main machine
            1000000.0, 0.1, 1.0, 0.01,
            # Firecracker VMs
            500000.0, 0.05, 0.5, 0.005,
            # DQ05
            800000.0, 0.02, 0.3, 0.003
        ])
```

#### 2.2 **Cross-Machine Resource Balancing**
**Goal**: Optimize workload distribution across machines

**Business Decision Logic**:
```bash
# Enhanced dispatch logic considering distributed resources
distribute_workload() {
    local workload_type=$1
    local resource_requirements=$2
    
    # Query Kalman predictions for all machines
    local predictions=$(get_distributed_kalman_predictions)
    
    # Select optimal machine based on:
    # 1. Resource availability (CPU, memory, network)
    # 2. Workload type (compute vs storage vs network intensive)
    # 3. Cost factors (firecracker costs, bandwidth)
    # 4. Latency requirements
    
    local best_machine=$(select_optimal_machine "$workload_type" "$predictions")
    dispatch_to_machine "$best_machine" "$workload"
}
```

### Phase 3: Distributed Business Decisions (Week 4)

#### 3.1 **Cost-Aware Resource Allocation**
**Goal**: Optimize business costs across distributed infrastructure

**Cost Model Integration**:
```yaml
infrastructure_costs:
  main_machine:
    cpu_per_hour_sats: 10
    memory_per_gb_hour_sats: 5
    network_per_gb_sats: 100
    
  firecracker_vms:
    vm1:
      cpu_per_hour_sats: 8     # Lower cost (lightweight)
      memory_per_gb_hour_sats: 4
      network_per_gb_sats: 120  # Higher (distributed)
    vm2:
      cpu_per_hour_sats: 8
      memory_per_gb_hour_sats: 4
      network_per_gb_sats: 120
      
  dq05_mini_pc:
    cpu_per_hour_sats: 12       # Higher cost (specialized)
    memory_per_gb_hour_sats: 6
    network_per_gb_sats: 80     # Lower (edge location)
    location_premium_sats: 50  # Edge computing premium
```

#### 3.2 **TollGate Provider Optimization with Distribution**
**Goal**: Select optimal network paths and providers across distributed nodes

**Distributed Network Selection**:
```python
class DistributedTollGateRouter:
    def __init__(self):
        self.nodes = {
            'main': {'latency': 0, 'bandwidth': '1Gbps', 'cost': 0.0},
            'vm1': {'latency': 5, 'bandwidth': '100Mbps', 'cost': 0.002},
            'vm2': {'latency': 5, 'bandwidth': '100Mbps', 'cost': 0.002},
            'dq05': {'latency': 2, 'bandwidth': '500Mbps', 'cost': 0.001}
        }
        
    def optimize_route(self, destination, requirements):
        """Select optimal route considering cost, latency, bandwidth"""
        return self.min_cost_path(destination, requirements)
```

### Phase 4: Distributed Dashboard Visualization (Week 4)

#### 4.1 **Multi-Machine Resource Dashboard**
**Goal**: Visualize distributed resources and workloads

**Enhanced Dashboard Charts**:
```javascript
// Add to build_v3.py
const distributedChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: timestamps,
        datasets: [
            // Main machine
            {
                label: 'Main CPU Load',
                data: main_cpu_load,
                borderColor: 'rgb(255, 99, 132)',
                yAxisID: 'y-axis-cpu'
            },
            // Firecracker VMs
            {
                label: 'VM1 CPU Load',
                data: vm1_cpu_load,
                borderColor: 'rgb(54, 162, 235)',
                yAxisID: 'y-axis-cpu'
            },
            {
                label: 'VM2 CPU Load',
                data: vm2_cpu_load,
                borderColor: 'rgb(255, 206, 86)',
                yAxisID: 'y-axis-cpu'
            },
            // DQ05
            {
                label: 'DQ05 CPU Load',
                data: dq05_cpu_load,
                borderColor: 'rgb(75, 192, 192)',
                yAxisID: 'y-axis-cpu'
            }
        ]
    },
    options: {
        scales: {
            'y-axis-cpu': {
                type: 'logarithmic',
                position: 'left',
                title: { display: true, text: 'CPU Load (log scale)' }
            }
        }
    }
});
```

#### 4.2 **Cost Optimization Heatmap**
**Goal**: Show cost efficiency across distributed resources

```javascript
const costEfficiencyChart = new Chart(ctx, {
    type: 'heatmap',
    data: {
        x: ['Main', 'VM1', 'VM2', 'DQ05'],
        y: ['CPU', 'Memory', 'Network', 'Total'],
        data: [
            // Cost efficiency (lower is better)
            [1.0, 0.8, 1.2, 0.7],  // Main machine
            [0.8, 0.8, 1.5, 0.9],  // VM1
            [0.8, 0.8, 1.5, 0.9],  // VM2
            [1.2, 1.2, 0.8, 1.0]   // DQ05
        ]
    },
    options: {
        title: { display: true, text: 'Cost Efficiency by Resource (Normalized)' }
    }
});
```

## Implementation Tasks for Kanban

### New Tasks to Add
1. **Phase 2.1.1: Create Distributed Machine Inventory**
2. **Phase 2.1.2: Extend Resource Collection for Distributed Systems**
3. **Phase 3.1.1: Implement Distributed Kalman Predictions**
4. **Phase 3.2.1: Add Cross-Machine Load Balancing**
5. **Phase 4.1.1: Create Distributed Resource Dashboard**

### Dependencies
- **Phase 2.1 (system resource collection)** must be completed first
- **Firecracker VM access** needs to be established
- **DQ05 connectivity** needs to be configured
- **Network paths** between machines must be optimized

## Success Metrics

### Short-term (2 weeks)
- [ ] All distributed machines monitored every 5 minutes
- [ ] Distributed resource data flowing to dashboard
- [ ] Basic cross-machine dispatch working

### Medium-term (3 weeks)
- [ ] Distributed Kalman predictions accurate
- [ ] Cost-aware resource allocation operational
- [ ] 15% improvement in resource utilization

### Long-term (4 weeks)
- [ ] Self-optimizing distributed resource allocation
- [ ] 25% reduction in infrastructure costs
- [ ] TollGate optimized for multi-node routing

## Next Steps

1. **Establish connectivity** to firecracker VMs and DQ05
2. **Create machine inventory** with capabilities and costs
3. **Extend resource collection** to distributed systems
4. **Update comprehensive plan** with distributed resource strategies

---

*Integration Date: 2026-07-08*
*Next Review: After distributed connectivity established*