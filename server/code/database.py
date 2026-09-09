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
        self._legacy_battery_percent_column = False
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
                    aggregator_name TEXT NOT NULL,
                    machine_type INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    rms REAL NOT NULL,
                    dominant_freq REAL NOT NULL,
                    battery_voltage REAL NOT NULL,
                    rssi INTEGER
                )
            ''')

            cursor.execute("PRAGMA table_info(readings)")
            reading_columns = {row['name'] for row in cursor.fetchall()}
            self._legacy_battery_percent_column = 'battery_percent' in reading_columns
            if 'battery_voltage' not in reading_columns:
                cursor.execute('ALTER TABLE readings ADD COLUMN battery_voltage REAL')
            if 'rssi' not in reading_columns:
                cursor.execute('ALTER TABLE readings ADD COLUMN rssi INTEGER')
            
            # State changes table - stores state transitions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS state_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    aggregator_name TEXT NOT NULL,
                    machine_type INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    old_state TEXT NOT NULL,
                    new_state TEXT NOT NULL
                )
            ''')
            
            # Cycles table - stores completed wash/dry cycles
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aggregator_name TEXT NOT NULL,
                    machine_type INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    duration_minutes REAL
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sensors (
                    sensor_id TEXT PRIMARY KEY,
                    assigned_aggregator_name TEXT,
                    machine_type INTEGER,
                    machine_id INTEGER,
                    assigned_at REAL,
                    last_aggregator_name TEXT,
                    last_seen_at REAL NOT NULL,
                    last_assignment_flag_at REAL,
                    assignment_active INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            ''')
            cursor.execute("PRAGMA table_info(sensors)")
            sensor_columns = {row['name'] for row in cursor.fetchall()}
            if 'assignment_active' not in sensor_columns:
                cursor.execute(
                    'ALTER TABLE sensors ADD COLUMN assignment_active '
                    'INTEGER NOT NULL DEFAULT 0'
                )
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sensor_assignment_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sensor_id TEXT NOT NULL,
                    old_aggregator_name TEXT,
                    old_machine_type INTEGER,
                    old_machine_id INTEGER,
                    new_aggregator_name TEXT,
                    new_machine_type INTEGER,
                    new_machine_id INTEGER,
                    changed_at REAL NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aggregators (
                    aggregator_name TEXT PRIMARY KEY,
                    last_seen_at REAL NOT NULL
                )
            ''')

            self._migrate_aggregator_identity(cursor)
            self._migrate_machine_type(cursor)
            
            # Create indexes for common queries
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_readings_time 
                ON readings(timestamp DESC)
            ''')
            cursor.execute('DROP INDEX IF EXISTS idx_readings_machine')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_readings_machine 
                ON readings(aggregator_name, machine_type, machine_id, timestamp DESC)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_state_changes_time 
                ON state_changes(timestamp DESC)
            ''')
            cursor.execute('DROP INDEX IF EXISTS idx_cycles_machine')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_cycles_machine 
                ON cycles(aggregator_name, machine_type, machine_id, start_time DESC)
            ''')
            cursor.execute('DROP INDEX IF EXISTS idx_sensor_assignments_machine')
            cursor.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sensor_assignments_machine
                ON sensors(assigned_aggregator_name, machine_type, machine_id)
                WHERE machine_id IS NOT NULL
            ''')
            
        logger.info(f"Database initialized: {self.db_path}")

    @staticmethod
    def _migrate_aggregator_identity(cursor):
        columns = {
            'readings': [('aggregator_id', 'aggregator_name')],
            'state_changes': [('aggregator_id', 'aggregator_name')],
            'cycles': [('aggregator_id', 'aggregator_name')],
            'sensors': [
                ('assigned_aggregator_id', 'assigned_aggregator_name'),
                ('last_aggregator_id', 'last_aggregator_name'),
            ],
            'sensor_assignment_history': [
                ('old_aggregator_id', 'old_aggregator_name'),
                ('new_aggregator_id', 'new_aggregator_name'),
            ],
        }
        for table, renames in columns.items():
            cursor.execute(f'PRAGMA table_info({table})')
            existing = {row['name'] for row in cursor.fetchall()}
            for old_name, new_name in renames:
                if old_name in existing and new_name not in existing:
                    cursor.execute(
                        f'ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}'
                    )
                    cursor.execute(
                        f"UPDATE {table} SET {new_name} = 'D' || {new_name} "
                        f'WHERE typeof({new_name}) = \'integer\''
                    )

        cursor.execute('PRAGMA table_info(aggregators)')
        aggregator_columns = {row['name'] for row in cursor.fetchall()}
        if 'aggregator_id' in aggregator_columns:
            cursor.execute('ALTER TABLE aggregators RENAME TO legacy_aggregators')
            cursor.execute('''
                CREATE TABLE aggregators (
                    aggregator_name TEXT PRIMARY KEY,
                    last_seen_at REAL NOT NULL
                )
            ''')
            cursor.execute('''
                INSERT INTO aggregators (aggregator_name, last_seen_at)
                SELECT 'D' || aggregator_id, last_seen_at FROM legacy_aggregators
            ''')
            cursor.execute('DROP TABLE legacy_aggregators')

    @staticmethod
    def _migrate_machine_type(cursor):
        for table in ('readings', 'state_changes', 'cycles'):
            cursor.execute(f'PRAGMA table_info({table})')
            columns = {row['name'] for row in cursor.fetchall()}
            if 'machine_type' not in columns:
                cursor.execute(
                    f'ALTER TABLE {table} ADD COLUMN machine_type INTEGER NOT NULL DEFAULT 0'
                )
                cursor.execute(f'''
                    UPDATE {table}
                    SET machine_type = COALESCE((
                        SELECT sensors.machine_type
                        FROM sensors
                        WHERE sensors.assigned_aggregator_name = {table}.aggregator_name
                          AND sensors.machine_id = {table}.machine_id
                    ), 0)
                ''')

    def record_sensor_observation(self, sensor_id: str, aggregator_name: str,
                                  assignment_active: bool, timestamp: float):
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO sensors (
                    sensor_id, last_aggregator_name, last_seen_at,
                    last_assignment_flag_at, assignment_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                    last_aggregator_name = excluded.last_aggregator_name,
                    last_seen_at = excluded.last_seen_at,
                    last_assignment_flag_at = CASE
                        WHEN excluded.assignment_active = 1
                            AND sensors.assignment_active = 0
                        THEN excluded.last_seen_at
                        ELSE sensors.last_assignment_flag_at END
                    , assignment_active = excluded.assignment_active
            ''', (sensor_id, aggregator_name, timestamp,
                  timestamp if assignment_active else None,
                  int(assignment_active), timestamp))

    def get_sensor_assignment(self, sensor_id: str) -> Optional[Dict]:
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT assigned_aggregator_name, machine_type, machine_id
                FROM sensors WHERE sensor_id = ?
            ''', (sensor_id,))
            row = cursor.fetchone()
            return dict(row) if row and row['machine_id'] is not None else None

    def get_sensors(self) -> List[Dict]:
        with self._cursor() as cursor:
            cursor.execute('''
                  SELECT sensor_id, assigned_aggregator_name, machine_type, machine_id,
                      assigned_at, last_aggregator_name, last_seen_at,
                       last_assignment_flag_at
                FROM sensors
                ORDER BY last_assignment_flag_at DESC, last_seen_at DESC
            ''')
            return [dict(row) for row in cursor.fetchall()]

    def get_assignments(self) -> List[Dict]:
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT sensor_id, assigned_aggregator_name, machine_type, machine_id
                FROM sensors
                WHERE assigned_aggregator_name IS NOT NULL AND machine_type IS NOT NULL
                    AND machine_id IS NOT NULL
                ORDER BY assigned_aggregator_name, machine_id
            ''')
            return [dict(row) for row in cursor.fetchall()]

    def record_aggregator_contact(self, aggregator_name: str, timestamp: float):
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO aggregators (aggregator_name, last_seen_at)
                VALUES (?, ?)
                ON CONFLICT(aggregator_name) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
            ''', (aggregator_name, timestamp))

    def get_live_aggregators(self, offline_minutes: int = 5) -> List[str]:
        cutoff = time.time() - offline_minutes * 60
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT aggregator_name
                FROM aggregators
                WHERE last_seen_at >= ?
                ORDER BY aggregator_name
            ''', (cutoff,))
            return [row['aggregator_name'] for row in cursor.fetchall()]

    def assign_sensor(self, sensor_id: str, aggregator_name: str, machine_type: int,
                      machine_id: int, timestamp: float):
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT assigned_aggregator_name, machine_type, machine_id
                FROM sensors WHERE sensor_id = ?
            ''', (sensor_id,))
            previous = cursor.fetchone()
            cursor.execute('''
                UPDATE sensors SET assigned_aggregator_name = ?, machine_type = ?,
                    machine_id = ?, assigned_at = ? WHERE sensor_id = ?
            ''', (aggregator_name, machine_type, machine_id, timestamp, sensor_id))
            if cursor.rowcount != 1:
                raise ValueError('Unknown sensor')
            cursor.execute('''
                INSERT INTO sensor_assignment_history (
                    sensor_id, old_aggregator_name, old_machine_type, old_machine_id,
                    new_aggregator_name, new_machine_type, new_machine_id, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sensor_id,
                  previous['assigned_aggregator_name'] if previous else None,
                  previous['machine_type'] if previous else None,
                  previous['machine_id'] if previous else None,
                  aggregator_name, machine_type, machine_id, timestamp))
        
    def store_reading(self, reading: MachineReading):
        """Store a sensor reading"""
        with self._cursor() as cursor:
            values = (
                reading.timestamp, reading.aggregator_name, reading.machine_type,
                reading.machine_id,
                reading.rms, reading.dominant_freq, reading.battery_voltage,
                reading.rssi
            )
            if self._legacy_battery_percent_column:
                encoded_voltage = round((reading.battery_voltage - 1.0) * 100)
                cursor.execute('''
                    INSERT INTO readings (
                        timestamp, aggregator_name, machine_type, machine_id, rms, dominant_freq,
                        battery_voltage, rssi, battery_percent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', values + (encoded_voltage,))
            else:
                cursor.execute('''
                    INSERT INTO readings (
                        timestamp, aggregator_name, machine_type, machine_id, rms, dominant_freq,
                        battery_voltage, rssi
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', values)
            
    def store_state_change(self, aggregator_name: str, machine_type: int, machine_id: int,
                          old_state: MachineState, new_state: MachineState):
        """Store a state change event"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO state_changes (timestamp, aggregator_name, machine_type, machine_id, old_state, new_state)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                time.time(),
                aggregator_name,
                machine_type,
                machine_id,
                old_state.value,
                new_state.value
            ))
            
    def start_cycle(self, aggregator_name: str, machine_type: int, machine_id: int) -> int:
        """Record start of a new cycle, return cycle ID"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO cycles (aggregator_name, machine_type, machine_id, start_time)
                VALUES (?, ?, ?, ?)
            ''', (aggregator_name, machine_type, machine_id, time.time()))
            return cursor.lastrowid
            
    def end_cycle(self, aggregator_name: str, machine_type: int, machine_id: int):
        """Record end of current cycle"""
        now = time.time()
        with self._cursor() as cursor:
            # Find the most recent unfinished cycle
            cursor.execute('''
                SELECT id, start_time FROM cycles 
                WHERE aggregator_name = ? AND machine_type = ? AND machine_id = ?
                    AND end_time IS NULL
                ORDER BY start_time DESC LIMIT 1
            ''', (aggregator_name, machine_type, machine_id))
            
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
                    f"Cycle ended for {aggregator_name}/{machine_id}: "
                    f"{duration:.1f} minutes"
                )
                
    def get_recent_readings(self, aggregator_name: str, machine_type: int, machine_id: int,
                           hours: float = 24) -> List[Dict]:
        """Get recent readings for a machine"""
        cutoff = time.time() - (hours * 3600)
        
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT timestamp, rms, dominant_freq, battery_voltage, rssi
                FROM readings
                WHERE aggregator_name = ? AND machine_type = ? AND machine_id = ?
                    AND timestamp > ?
                    AND battery_voltage IS NOT NULL
                ORDER BY timestamp DESC
            ''', (aggregator_name, machine_type, machine_id, cutoff))
            
            return [dict(row) for row in cursor.fetchall()]
            
    def get_cycle_history(self, aggregator_name: str, machine_type: int, machine_id: int,
                         limit: int = 50) -> List[Dict]:
        """Get cycle history for a machine"""
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT start_time, end_time, duration_minutes
                FROM cycles
                WHERE aggregator_name = ? AND machine_type = ? AND machine_id = ?
                    AND end_time IS NOT NULL
                ORDER BY start_time DESC
                LIMIT ?
            ''', (aggregator_name, machine_type, machine_id, limit))
            
            return [dict(row) for row in cursor.fetchall()]
            
    def get_daily_stats(self, aggregator_name: str = None, days: int = 7) -> List[Dict]:
        """Get daily usage statistics"""
        cutoff = time.time() - (days * 86400)
        
        with self._cursor() as cursor:
            if aggregator_name:
                cursor.execute('''
                    SELECT 
                        date(timestamp, 'unixepoch', 'localtime') as date,
                        COUNT(*) as cycle_count,
                        AVG(duration_minutes) as avg_duration,
                        SUM(duration_minutes) as total_duration
                    FROM cycles
                    WHERE aggregator_name = ? AND start_time > ? AND end_time IS NOT NULL
                    GROUP BY date
                    ORDER BY date DESC
                ''', (aggregator_name, cutoff))
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
