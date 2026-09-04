import unittest
from unittest.mock import patch

from lora_receiver import MachineReading
from state_machine import MachineState, StateMachine, Thresholds


class RunningConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.state = StateMachine(Thresholds(running_rms=0.5), {})
        self.state.load_assignments([{
            'assigned_aggregator_name': 'D2',
            'machine_type': 1,
            'machine_id': 2,
        }])

    @staticmethod
    def reading(timestamp, rms):
        return MachineReading(
            aggregator_name='D2', sensor_id='A1', assignment_active=False,
            rms=rms, dominant_freq=0, battery_voltage=3.0,
            timestamp=timestamp, machine_type=1, machine_id=2
        )

    def machine_state(self):
        return self.state.get_machine_status('D2', 1, 2)['state']

    def test_third_high_reading_within_minute_enters_running(self):
        with patch('state_machine.time.time', return_value=150):
            self.state.update(self.reading(100, 0.6))
            self.assertEqual(self.machine_state(), MachineState.UNKNOWN.value)
            self.state.update(self.reading(125, 0.7))
            self.assertEqual(self.machine_state(), MachineState.UNKNOWN.value)
            self.state.update(self.reading(150, 0.8))
            self.assertEqual(self.machine_state(), MachineState.RUNNING.value)

    def test_high_readings_older_than_minute_do_not_count(self):
        with patch('state_machine.time.time', return_value=181):
            self.state.update(self.reading(100, 0.6))
            self.state.update(self.reading(150, 0.7))
            self.state.update(self.reading(181, 0.8))
            self.assertEqual(self.machine_state(), MachineState.UNKNOWN.value)

    def test_reading_equal_to_threshold_does_not_count(self):
        with patch('state_machine.time.time', return_value=140):
            self.state.update(self.reading(100, 0.6))
            self.state.update(self.reading(120, 0.5))
            self.state.update(self.reading(140, 0.7))
            self.assertEqual(self.machine_state(), MachineState.FREE.value)

    def test_running_machine_stays_running_on_next_high_reading(self):
        with patch('state_machine.time.time', return_value=140):
            self.state.update(self.reading(100, 0.6))
            self.state.update(self.reading(120, 0.7))
            self.state.update(self.reading(140, 0.8))
        with patch('state_machine.time.time', return_value=220):
            self.state.update(self.reading(220, 0.9))
            self.assertEqual(self.machine_state(), MachineState.RUNNING.value)


if __name__ == '__main__':
    unittest.main()