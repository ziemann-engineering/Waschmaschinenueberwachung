"""
State Machine Module
Tracks machine states and determines availability
"""

import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from threading import Lock
import logging

from lora_receiver import MachineReading

logger = logging.getLogger(__name__)


class MachineState(Enum):
    """Machine state enumeration"""
    UNKNOWN = "unknown"     # No data yet
    RUNNING = "running"     # Vibration above threshold
    STOPPING = "stopping"   # Below threshold < done_minutes
    DONE = "done"          # Below threshold >= done_minutes, < free_minutes
    FREE = "free"          # Below threshold >= free_minutes
    OFFLINE = "offline"     # No data received recently


# State display properties (German labels)
STATE_INFO = {
    MachineState.UNKNOWN: {"color": "gray", "label": "Nicht gefunden", "icon": "❓"},
    MachineState.RUNNING: {"color": "red", "label": "Läuft", "icon": "🔴"},
    MachineState.STOPPING: {"color": "yellow", "label": "Stoppt", "icon": "🟡"},
    MachineState.DONE: {"color": "yellow-green", "label": "Wahrsch. fertig", "icon": "🟢"},
    MachineState.FREE: {"color": "blue-green", "label": "Frei", "icon": "🔵"},
    MachineState.OFFLINE: {"color": "gray", "label": "Offline", "icon": "⚫"},
}


@dataclass
class MachineStatus:
    """Complete status for a single machine"""
    aggregator_id: int
    machine_id: int
    name: str
    machine_type: int = 1             # 1=washer, 2=dryer
    state: MachineState = MachineState.UNKNOWN
    rms: float = 0.0
    dominant_freq: float = 0.0
    battery_percent: int = 100
    last_reading_time: float = 0.0
    last_running_time: float = 0.0      # When it was last running
    state_change_time: float = 0.0      # When state last changed
    cycle_start_time: Optional[float] = None  # When current/last cycle started
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        now = time.time()
        state_info = STATE_INFO[self.state]
        
        # Calculate time since state change
        if self.state_change_time > 0:
            time_in_state = now - self.state_change_time
        else:
            time_in_state = 0
            
        # Calculate cycle duration if applicable
        if self.cycle_start_time:
            cycle_duration = now - self.cycle_start_time
        else:
            cycle_duration = 0
            
        # Check if we have real data
        has_data = self.last_reading_time > 0
        
        # Check for low battery (only if we have data)
        low_battery = has_data and self.battery_percent < 20
        
        return {
            "aggregator_id": self.aggregator_id,
            "machine_id": self.machine_id,
            "name": self.name,
            "machine_type": self.machine_type,
            "type_label": "Waschmaschine" if self.machine_type == 1 else "Tumbler",
            "state": self.state.value,
            "state_label": state_info["label"],
            "state_with_time": self._format_state_with_time(state_info["label"], time_in_state),
            "state_color": state_info["color"],
            "state_icon": state_info["icon"],
            "has_data": has_data,
            "low_battery": low_battery,
            "rms": round(self.rms, 3) if has_data else None,
            "rms_display": f"{round(self.rms, 3)}" if has_data else "?",
            "dominant_freq": round(self.dominant_freq, 1),
            "battery_percent": self.battery_percent if has_data else None,
            "battery_display": f"{self.battery_percent}%" if has_data else "?",
            "last_reading_time": self.last_reading_time,
            "time_since_reading": round(now - self.last_reading_time) if self.last_reading_time > 0 else None,
            "time_in_state_seconds": round(time_in_state),
            "time_in_state_formatted": self._format_duration(time_in_state),
            "cycle_duration_seconds": round(cycle_duration) if cycle_duration > 0 else None,
            "cycle_duration_formatted": self._format_duration(cycle_duration) if cycle_duration > 0 else None,
        }
        
    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration as human-readable string"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds / 3600)
            minutes = int((seconds % 3600) / 60)
            return f"{hours}h {minutes}m"
    
    @staticmethod
    def _format_state_with_time(state_label: str, seconds: float) -> str:
        """Format state label with time since change"""
        if seconds <= 0:
            return state_label
        elif seconds >= 86400:  # > 24 hours
            return f"{state_label}, seit > 24h"
        elif seconds >= 3600:  # >= 1 hour
            hours = int(seconds / 3600)
            return f"{state_label}, seit {hours}h"
        else:  # < 1 hour, show minutes
            minutes = max(1, int(seconds / 60))
            return f"{state_label}, seit {minutes}min"


@dataclass
class Thresholds:
    """Threshold configuration"""
    running_rms: float = 0.5        # RMS above this = running
    done_minutes: int = 10          # Minutes below threshold = done
    free_minutes: int = 120         # Minutes below threshold = free
    offline_minutes: int = 5        # Minutes without data = offline


class StateMachine:
    """
    Manages state for all machines.
    Thread-safe for concurrent access.
    """
    
    def __init__(self, thresholds: Thresholds, config: dict):
        self.thresholds = thresholds
        self.config = config
        self.machines: Dict[Tuple[int, int], MachineStatus] = {}
        self.lock = Lock()
        self._init_machines_from_config()
        
    def _init_machines_from_config(self):
        """Initialize machine entries from config"""
        aggregators = self.config.get("aggregators", {})
        
        for agg_id_str, agg_config in aggregators.items():
            agg_id = int(agg_id_str)
            machines = agg_config.get("machines", {})
            
            for machine_id_str, machine_info in machines.items():
                machine_id = int(machine_id_str)
                key = (agg_id, machine_id)
                
                # Support both old format (string) and new format (dict with type)
                if isinstance(machine_info, str):
                    machine_name = machine_info
                    machine_type = 1  # default to washer
                else:
                    machine_name = machine_info.get("name", f"Machine {machine_id}")
                    machine_type = machine_info.get("type", 1)
                
                self.machines[key] = MachineStatus(
                    aggregator_id=agg_id,
                    machine_id=machine_id,
                    name=machine_name,
                    machine_type=machine_type
                )
                
        logger.info(f"Initialized {len(self.machines)} machines from config")
        
    def update(self, reading: MachineReading):
        """Update machine state based on new reading"""
        key = (reading.aggregator_id, reading.machine_id)
        now = time.time()
        
        with self.lock:
            # Get or create machine status
            if key not in self.machines:
                # Unknown machine, create entry (use type from reading if available)
                machine_type = getattr(reading, 'machine_type', 1)
                self.machines[key] = MachineStatus(
                    aggregator_id=reading.aggregator_id,
                    machine_id=reading.machine_id,
                    name=f"Machine {reading.machine_id}",
                    machine_type=machine_type
                )
                logger.warning(
                    f"Unknown machine {reading.aggregator_id}/{reading.machine_id}, "
                    "created new entry"
                )
                
            machine = self.machines[key]
            old_state = machine.state
            
            # Update readings
            machine.rms = reading.rms
            machine.dominant_freq = reading.dominant_freq
            machine.battery_percent = reading.battery_percent
            machine.last_reading_time = reading.timestamp
            
            # Determine new state
            is_running = reading.rms >= self.thresholds.running_rms
            
            if is_running:
                new_state = MachineState.RUNNING
                machine.last_running_time = now
                
                # Start new cycle if coming from FREE or DONE
                if old_state in (MachineState.FREE, MachineState.DONE, MachineState.UNKNOWN):
                    machine.cycle_start_time = now
                    logger.info(f"Machine {key} started new cycle")
            else:
                # Calculate time since last running
                if machine.last_running_time > 0:
                    idle_minutes = (now - machine.last_running_time) / 60.0
                else:
                    idle_minutes = float('inf')  # Never seen running
                    
                if idle_minutes < self.thresholds.done_minutes:
                    new_state = MachineState.STOPPING
                elif idle_minutes < self.thresholds.free_minutes:
                    new_state = MachineState.DONE
                else:
                    new_state = MachineState.FREE
                    machine.cycle_start_time = None  # Clear cycle
                    
            # Update state if changed
            if new_state != old_state:
                machine.state = new_state
                machine.state_change_time = now
                logger.info(
                    f"Machine {key} state: {old_state.value} -> {new_state.value}"
                )
                
                # Return state change info for notifications
                return (machine, old_state, new_state)
                
        return None
        
    def check_offline(self) -> list:
        """Check for machines that have gone offline"""
        now = time.time()
        offline_threshold = self.thresholds.offline_minutes * 60
        state_changes = []
        
        with self.lock:
            for machine in self.machines.values():
                if machine.state != MachineState.OFFLINE:
                    if machine.last_reading_time > 0:
                        time_since = now - machine.last_reading_time
                        if time_since > offline_threshold:
                            old_state = machine.state
                            machine.state = MachineState.OFFLINE
                            machine.state_change_time = now
                            logger.warning(
                                f"Machine {machine.aggregator_id}/{machine.machine_id} "
                                f"went offline (no data for {time_since:.0f}s)"
                            )
                            state_changes.append((machine, old_state, MachineState.OFFLINE))
                            
        return state_changes
        
    def get_all_status(self) -> Dict[str, list]:
        """Get status of all machines grouped by aggregator"""
        result = {}
        
        with self.lock:
            for (agg_id, _), machine in sorted(self.machines.items()):
                agg_key = str(agg_id)
                if agg_key not in result:
                    agg_config = self.config.get("aggregators", {}).get(agg_key, {})
                    result[agg_key] = {
                        "id": agg_id,
                        "name": agg_config.get("name", f"Aggregator {agg_id}"),
                        "location": agg_config.get("location", ""),
                        "machines": [],
                        "washers": [],      # Separate list for washers
                        "dryers": [],       # Separate list for dryers
                        "summary": {
                            "total": 0,
                            "free": 0,
                            "done": 0,
                            "running": 0,
                            "offline": 0,
                            "washers_total": 0,
                            "washers_free": 0,
                            "washers_running": 0,
                            "dryers_total": 0,
                            "dryers_free": 0,
                            "dryers_running": 0
                        }
                    }
                
                machine_dict = machine.to_dict()
                result[agg_key]["machines"].append(machine_dict)
                result[agg_key]["summary"]["total"] += 1
                
                # Add to type-specific lists
                is_washer = machine.machine_type == 1
                if is_washer:
                    result[agg_key]["washers"].append(machine_dict)
                    result[agg_key]["summary"]["washers_total"] += 1
                else:
                    result[agg_key]["dryers"].append(machine_dict)
                    result[agg_key]["summary"]["dryers_total"] += 1
                
                # Update state counts
                if machine.state == MachineState.FREE:
                    result[agg_key]["summary"]["free"] += 1
                    if is_washer:
                        result[agg_key]["summary"]["washers_free"] += 1
                    else:
                        result[agg_key]["summary"]["dryers_free"] += 1
                elif machine.state == MachineState.DONE:
                    result[agg_key]["summary"]["done"] += 1
                    if is_washer:
                        result[agg_key]["summary"]["washers_free"] += 1
                    else:
                        result[agg_key]["summary"]["dryers_free"] += 1
                elif machine.state == MachineState.RUNNING:
                    result[agg_key]["summary"]["running"] += 1
                    if is_washer:
                        result[agg_key]["summary"]["washers_running"] += 1
                    else:
                        result[agg_key]["summary"]["dryers_running"] += 1
                elif machine.state == MachineState.OFFLINE:
                    result[agg_key]["summary"]["offline"] += 1
            
            # Determine if aggregator is online (at least one machine has data)
            for agg_key in result:
                all_unknown_or_offline = all(
                    m["state"] in ("unknown", "offline") 
                    for m in result[agg_key]["machines"]
                )
                result[agg_key]["online"] = not all_unknown_or_offline
                    
        return result
        
    def get_aggregator_status(self, aggregator_id: int) -> Optional[dict]:
        """Get status for a specific aggregator"""
        all_status = self.get_all_status()
        return all_status.get(str(aggregator_id))
        
    def get_machine_status(self, aggregator_id: int, machine_id: int) -> Optional[dict]:
        """Get status for a specific machine"""
        key = (aggregator_id, machine_id)
        
        with self.lock:
            if key in self.machines:
                return self.machines[key].to_dict()
                
        return None
