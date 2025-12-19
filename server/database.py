"""
Database Module
SQLite storage for machine history and statistics
"""

import sqlite3
import time
import threading
from typing import Optional, List, Dict
from contextlib import contextmanager
import logging

from lora_receiver import MachineReading
from state_machine import MachineState

logger = logging.getLogger(__name__)


class Database:
    """SQLite database for storing machine history"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.local = threading.local()
        self._init_db()
        
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection"""
        if not hasattr(self.local, 'conn') or self.local.conn is None:
            self.local.conn = sqlite3.connect(self.db_path)
            self.local.conn.row_factory = sqlite3.Row
        return self.local.conn
        
    @contextmanager
    def _cursor(self):
        """Context manager for database cursor"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
            
    def _init_db(self):
        """Initialize database schema"""
        with self._cursor() as cursor:
            # Readings table - stores all received readings
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    aggregator_id INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    rms REAL NOT NULL,
                    dominant_freq REAL NOT NULL,
                    battery_percent INTEGER NOT NULL
                )
            ''')
            
            # State changes table - stores state transitions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS state_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    aggregator_id INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    old_state TEXT NOT NULL,
                    new_state TEXT NOT NULL
                )
            ''')
            
            # Cycles table - stores completed wash/dry cycles
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aggregator_id INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    duration_minutes REAL
                )
            ''')
            
            # Create indexes for common queries
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_readings_time 
                ON readings(timestamp DESC)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_readings_machine 
                ON readings(aggregator_id, machine_id, timestamp DESC)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_state_changes_time 
                ON state_changes(timestamp DESC)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_cycles_machine 
                ON cycles(aggregator_id, machine_id, start_time DESC)
            ''')
            
        logger.info(f"Database initialized: {self.db_path}")
        
    def store_reading(self, reading: MachineReading):
        """Store a sensor reading"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO readings (timestamp, aggregator_id, machine_id, rms, dominant_freq, battery_percent)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                reading.timestamp,
                reading.aggregator_id,
                reading.machine_id,
                reading.rms,
                reading.dominant_freq,
                reading.battery_percent
            ))
            
    def store_state_change(self, aggregator_id: int, machine_id: int, 
                          old_state: MachineState, new_state: MachineState):
        """Store a state change event"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO state_changes (timestamp, aggregator_id, machine_id, old_state, new_state)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                time.time(),
                aggregator_id,
                machine_id,
                old_state.value,
                new_state.value
            ))
            
    def start_cycle(self, aggregator_id: int, machine_id: int) -> int:
        """Record start of a new cycle, return cycle ID"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO cycles (aggregator_id, machine_id, start_time)
                VALUES (?, ?, ?)
            ''', (aggregator_id, machine_id, time.time()))
            return cursor.lastrowid
            
    def end_cycle(self, aggregator_id: int, machine_id: int):
        """Record end of current cycle"""
        now = time.time()
        with self._cursor() as cursor:
            # Find the most recent unfinished cycle
            cursor.execute('''
                SELECT id, start_time FROM cycles 
                WHERE aggregator_id = ? AND machine_id = ? AND end_time IS NULL
                ORDER BY start_time DESC LIMIT 1
            ''', (aggregator_id, machine_id))
            
            row = cursor.fetchone()
            if row:
                cycle_id = row['id']
                start_time = row['start_time']
                duration = (now - start_time) / 60.0  # minutes
                
                cursor.execute('''
                    UPDATE cycles SET end_time = ?, duration_minutes = ?
                    WHERE id = ?
                ''', (now, duration, cycle_id))
                
                logger.info(
                    f"Cycle ended for {aggregator_id}/{machine_id}: "
                    f"{duration:.1f} minutes"
                )
                
    def get_recent_readings(self, aggregator_id: int, machine_id: int, 
                           hours: float = 24) -> List[Dict]:
        """Get recent readings for a machine"""
        cutoff = time.time() - (hours * 3600)
        
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT timestamp, rms, dominant_freq, battery_percent
                FROM readings
                WHERE aggregator_id = ? AND machine_id = ? AND timestamp > ?
                ORDER BY timestamp DESC
            ''', (aggregator_id, machine_id, cutoff))
            
            return [dict(row) for row in cursor.fetchall()]
            
    def get_cycle_history(self, aggregator_id: int, machine_id: int,
                         limit: int = 50) -> List[Dict]:
        """Get cycle history for a machine"""
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT start_time, end_time, duration_minutes
                FROM cycles
                WHERE aggregator_id = ? AND machine_id = ? AND end_time IS NOT NULL
                ORDER BY start_time DESC
                LIMIT ?
            ''', (aggregator_id, machine_id, limit))
            
            return [dict(row) for row in cursor.fetchall()]
            
    def get_daily_stats(self, aggregator_id: int = None, days: int = 7) -> List[Dict]:
        """Get daily usage statistics"""
        cutoff = time.time() - (days * 86400)
        
        with self._cursor() as cursor:
            if aggregator_id:
                cursor.execute('''
                    SELECT 
                        date(timestamp, 'unixepoch', 'localtime') as date,
                        COUNT(*) as cycle_count,
                        AVG(duration_minutes) as avg_duration,
                        SUM(duration_minutes) as total_duration
                    FROM cycles
                    WHERE aggregator_id = ? AND start_time > ? AND end_time IS NOT NULL
                    GROUP BY date
                    ORDER BY date DESC
                ''', (aggregator_id, cutoff))
            else:
                cursor.execute('''
                    SELECT 
                        date(timestamp, 'unixepoch', 'localtime') as date,
                        COUNT(*) as cycle_count,
                        AVG(duration_minutes) as avg_duration,
                        SUM(duration_minutes) as total_duration
                    FROM cycles
                    WHERE start_time > ? AND end_time IS NOT NULL
                    GROUP BY date
                    ORDER BY date DESC
                ''', (cutoff,))
                
            return [dict(row) for row in cursor.fetchall()]
            
    def cleanup_old_data(self, days: int = 30):
        """Remove data older than specified days"""
        cutoff = time.time() - (days * 86400)
        
        with self._cursor() as cursor:
            cursor.execute('DELETE FROM readings WHERE timestamp < ?', (cutoff,))
            deleted_readings = cursor.rowcount
            
            cursor.execute('DELETE FROM state_changes WHERE timestamp < ?', (cutoff,))
            deleted_changes = cursor.rowcount
            
            cursor.execute('DELETE FROM cycles WHERE end_time < ?', (cutoff,))
            deleted_cycles = cursor.rowcount
            
        logger.info(
            f"Cleanup: removed {deleted_readings} readings, "
            f"{deleted_changes} state changes, {deleted_cycles} cycles"
        )
