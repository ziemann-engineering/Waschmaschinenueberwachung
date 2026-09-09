import os
import sqlite3
import tempfile
import unittest

from database import Database
from lora_receiver import MachineReading
from state_machine import StateMachine, Thresholds


class MachineIdentityTest(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix='.db')

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_washer_and_dryer_can_share_number(self):
        database = Database(self.db_path)
        database.record_sensor_observation('W', 'D2', False, 1)
        database.record_sensor_observation('T', 'D2', False, 1)
        database.assign_sensor('W', 'D2', 1, 2, 1)
        database.assign_sensor('T', 'D2', 2, 2, 1)

        for sensor_id, machine_type, rms, rssi in (
            ('W', 1, 1.1, -51), ('T', 2, 2.2, -62)
        ):
            database.store_reading(MachineReading(
                aggregator_name='D2', sensor_id=sensor_id,
                assignment_active=False, rms=rms, dominant_freq=0,
                battery_voltage=3.0, rssi=rssi, timestamp=10,
                machine_type=machine_type, machine_id=2
            ))

        washer_history = database.get_recent_readings('D2', 1, 2, 1e9)
        dryer_history = database.get_recent_readings('D2', 2, 2, 1e9)
        self.assertEqual(washer_history[0]['rms'], 1.1)
        self.assertEqual(washer_history[0]['rssi'], -51)
        self.assertEqual(dryer_history[0]['rms'], 2.2)
        self.assertEqual(dryer_history[0]['rssi'], -62)

        state = StateMachine(Thresholds(), {})
        state.load_assignments(database.get_assignments())
        self.assertEqual(state.get_machine_status('D2', 1, 2)['machine_key'], 'W2')
        self.assertEqual(state.get_machine_status('D2', 2, 2)['machine_key'], 'T2')
        database.local.conn.close()

    def test_existing_history_gains_assigned_machine_type(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript('''
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY, timestamp REAL NOT NULL,
                aggregator_name TEXT NOT NULL, machine_id INTEGER NOT NULL,
                rms REAL NOT NULL, dominant_freq REAL NOT NULL,
                battery_voltage REAL, rssi INTEGER
            );
            CREATE TABLE sensors (
                sensor_id TEXT PRIMARY KEY, assigned_aggregator_name TEXT,
                machine_type INTEGER, machine_id INTEGER, assigned_at REAL,
                last_aggregator_name TEXT, last_seen_at REAL NOT NULL,
                last_assignment_flag_at REAL,
                assignment_active INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            INSERT INTO sensors VALUES
                ('W', 'D2', 1, 2, 0, 'D2', 1, NULL, 0, 0);
            INSERT INTO readings VALUES
                (1, 1, 'D2', 2, 1.5, 0, 3, -50);
        ''')
        connection.close()

        database = Database(self.db_path)
        connection = database._get_connection()
        machine_type = connection.execute(
            'SELECT machine_type FROM readings'
        ).fetchone()[0]
        index_columns = [row[2] for row in connection.execute(
            'PRAGMA index_info(idx_sensor_assignments_machine)'
        )]
        self.assertEqual(machine_type, 1)
        self.assertEqual(index_columns, [
            'assigned_aggregator_name', 'machine_type', 'machine_id'
        ])
        database.local.conn.close()


if __name__ == '__main__':
    unittest.main()