"""
Washing Machine Monitoring Server
Main entry point
"""

import json
import os
import sqlite3
import time
import threading
import argparse
import logging
from pathlib import Path

from flask import Flask, render_template, jsonify, request, abort, Response
from werkzeug.security import check_password_hash

from lora_receiver import MachineReading, decode_forwarded_packet
from state_machine import StateMachine, Thresholds, MachineState
from database import Database
from notifications import NotificationManager, Subscription

# ============================================================================
# Logging Setup
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

def load_config(config_path: str) -> dict:
    """Load configuration from JSON file"""
    with open(config_path, 'r') as f:
        return json.load(f)

# ============================================================================
# Flask App
# ============================================================================

app = Flask(__name__)
state_machine: StateMachine = None
database: Database = None
notification_manager: NotificationManager = None
config: dict = None
last_receiver_contact: float = 0


def admin_authenticated() -> bool:
    password_hash = os.getenv('ADMIN_PASSWORD_HASH')
    authorization = request.authorization
    return bool(password_hash and authorization and
                authorization.username == os.getenv('ADMIN_USERNAME', 'admin') and
                check_password_hash(password_hash, authorization.password))


def require_admin():
    if not os.getenv('ADMIN_PASSWORD_HASH'):
        abort(503, 'Admin password is not configured')
    if not admin_authenticated():
        return Response('Authentication required', 401,
                        {'WWW-Authenticate': 'Basic realm="WMS admin"'})
    return None


def format_timestamp(timestamp):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp)) if timestamp else '-'


def is_receiver_connected() -> bool:
    return (time.time() - last_receiver_contact) < 180


def parse_machine_key(machine_key: str):
    if len(machine_key) < 2 or machine_key[0].upper() not in ('W', 'T'):
        abort(404)
    try:
        machine_id = int(machine_key[1:])
    except ValueError:
        abort(404)
    if machine_id < 1:
        abort(404)
    machine_type = 1 if machine_key[0].upper() == 'W' else 2
    return machine_type, machine_id


@app.route('/')
def index():
    """Main page - all aggregators"""
    status = state_machine.get_all_status(database.get_live_aggregators())
    return render_template('index.html', aggregators=status, config=config,
                           receiver_connected=is_receiver_connected())


@app.route('/info')
def info_page():
    """Info page about the project"""
    return render_template('info.html')


@app.route('/admin/sensors')
def admin_sensors_page():
    authentication_error = require_admin()
    if authentication_error:
        return authentication_error
    return render_template('admin_sensors.html', sensors=database.get_sensors(),
                           format_timestamp=format_timestamp)


@app.route('/api/admin/sensors/<sensor_id>/assignment', methods=['POST'])
def api_assign_sensor(sensor_id: str):
    authentication_error = require_admin()
    if authentication_error:
        return authentication_error
    data = request.get_json(silent=True) or {}
    try:
        aggregator_name = str(data['aggregator_name']).strip()
        machine_type = int(data['machine_type'])
        machine_id = int(data['machine_id'])
    except (KeyError, TypeError, ValueError):
        abort(400, 'aggregator_name, machine_type, and machine_id are required')

    if not aggregator_name or len(aggregator_name.encode('utf-8')) > 32:
        abort(400, 'aggregator_name must encode to 1-32 bytes')
    if machine_id < 1 or machine_type not in (1, 2):
        abort(400, 'machine_id must be positive; machine_type must be 1 or 2')
    try:
        database.assign_sensor(sensor_id, aggregator_name, machine_type, machine_id, time.time())
    except sqlite3.IntegrityError:
        abort(409, 'That machine is already assigned to another sensor')
    except ValueError:
        abort(404, 'Unknown sensor')
    state_machine.load_assignments([{
        'assigned_aggregator_name': aggregator_name,
        'machine_type': machine_type,
        'machine_id': machine_id,
    }])
    return jsonify({'success': True})


@app.route('/<aggregator_name>/<machine_key>')
def machine_history_page(aggregator_name: str, machine_key: str):
    """Machine sensor history page."""
    machine_type, machine_id = parse_machine_key(machine_key)
    machine = state_machine.get_machine_status(aggregator_name, machine_type, machine_id)
    if not machine:
        abort(404)

    return render_template(
        'machine_history.html',
        machine=machine,
        aggregator_name=aggregator_name
    )


@app.route('/<aggregator_name>')
def aggregator_page(aggregator_name: str):
    """Single live aggregator page."""
    status = state_machine.get_aggregator_status(
        aggregator_name, database.get_live_aggregators()
    )
    if not status:
        abort(404)
    return render_template('aggregator.html', aggregator=status, config=config)


@app.route('/api/status')
def api_status():
    """API endpoint - all status data"""
    return jsonify(state_machine.get_all_status(database.get_live_aggregators()))


@app.route('/api/health')
def api_health():
    """Lightweight endpoint for service health checks."""
    if database is None or state_machine is None:
        return jsonify({'status': 'starting'}), 503
    return jsonify({'status': 'ok'})


@app.route('/api/aggregator/<aggregator_name>')
def api_aggregator(aggregator_name: str):
    """API endpoint - single aggregator status"""
    status = state_machine.get_aggregator_status(
        aggregator_name, database.get_live_aggregators()
    )
    if not status:
        abort(404)
    return jsonify(status)


@app.route('/api/machine/<aggregator_name>/<machine_key>')
def api_machine(aggregator_name: str, machine_key: str):
    """API endpoint - single machine status"""
    machine_type, machine_id = parse_machine_key(machine_key)
    status = state_machine.get_machine_status(aggregator_name, machine_type, machine_id)
    if not status:
        abort(404)
    return jsonify(status)


@app.route('/api/history/<aggregator_name>/<machine_key>')
def api_history(aggregator_name: str, machine_key: str):
    """API endpoint - machine reading history"""
    machine_type, machine_id = parse_machine_key(machine_key)
    hours = request.args.get('hours', 24, type=float)
    readings = database.get_recent_readings(
        aggregator_name, machine_type, machine_id, hours
    )
    cycles = database.get_cycle_history(
        aggregator_name, machine_type, machine_id, 20
    )
    return jsonify({
        'readings': readings,
        'cycles': cycles
    })


@app.route('/api/subscribe', methods=['POST'])
def api_subscribe():
    """Subscribe to notifications"""
    data = request.json
    
    import uuid
    sub = Subscription(
        id=str(uuid.uuid4()),
        email=data.get('email'),
        webhook_url=data.get('webhook_url'),
        watch_aggregator=data.get('aggregator_name'),
        watch_machine=data.get('machine_id'),
        notify_on_done=data.get('notify_on_done', True),
        notify_on_free=data.get('notify_on_free', False),
        notify_any_free=data.get('notify_any_free', False)
    )
    
    sub_id = notification_manager.add_subscription(sub)
    return jsonify({'subscription_id': sub_id})


@app.route('/api/unsubscribe/<subscription_id>', methods=['DELETE'])
def api_unsubscribe(subscription_id: str):
    """Unsubscribe from notifications"""
    if notification_manager.remove_subscription(subscription_id):
        return jsonify({'success': True})
    abort(404)


@app.route('/api/lora-data', methods=['POST'])
def api_lora_data():
    """HTTP endpoint to receive LoRa data from WiFi bridge"""
    global last_receiver_contact

    try:
        last_receiver_contact = time.time()
        data = request.json

        if data and data.get('keepalive'):
            logger.debug("Received keepalive from WiFi bridge")
            return jsonify({'success': True, 'type': 'keepalive'})

        if not data or 'packet_data' not in data:
            return jsonify({'error': 'Missing packet_data'}), 400
        
        # Decode the hex packet data
        try:
            packet_data = bytes.fromhex(data['packet_data'])
        except Exception as e:
            return jsonify({'error': f'Invalid hex packet data: {e}'}), 400
        
        try:
            aggregator_name, readings = decode_forwarded_packet(packet_data)
        except ValueError as exception:
            return jsonify({'error': str(exception)}), 400

        logger.info(
            f"Received HTTP LoRa packet: aggregator={aggregator_name}, "
            f"machines={len(readings)}"
        )
        database.record_aggregator_contact(aggregator_name, time.time())

        if not readings:
            logger.info(f"Received heartbeat from aggregator {aggregator_name}")
            return jsonify({'success': True, 'type': 'heartbeat'})

        for reading in readings:
            
            # Process the reading (same as LoRa callback)
            on_reading_received(reading)
            
            logger.info(
                f"Sensor {reading.sensor_id}: "
                f"RMS={reading.rms:.2f} m/s², "
                f"Freq={reading.dominant_freq:.1f} Hz, "
                f"Batt={reading.battery_voltage:.2f} V, "
                f"RSSI={reading.rssi} dBm"
            )
            
        return jsonify({
            'success': True, 
            'type': 'data',
            'readings_processed': len(readings)
        })
        
    except Exception as e:
        logger.exception(f"Error processing LoRa data: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Data Processing
# ============================================================================

def on_reading_received(reading: MachineReading):
    """Callback when a sensor reading is received"""
    database.record_sensor_observation(
        reading.sensor_id, reading.aggregator_name, reading.assignment_active,
        reading.timestamp
    )
    assignment = database.get_sensor_assignment(reading.sensor_id)
    if not assignment or assignment['assigned_aggregator_name'] != reading.aggregator_name:
        return
    reading.machine_type = assignment['machine_type']
    reading.machine_id = assignment['machine_id']

    # Store in database
    database.store_reading(reading)
    
    # Update state machine
    state_change = state_machine.update(reading)
    
    # Handle state changes
    if state_change:
        machine, old_state, new_state = state_change
        
        # Store state change
        database.store_state_change(
            machine.aggregator_name,
            machine.machine_type,
            machine.machine_id,
            old_state,
            new_state
        )
        
        # Track cycles
        if new_state == MachineState.RUNNING and old_state in (
            MachineState.FREE, MachineState.DONE, MachineState.UNKNOWN
        ):
            database.start_cycle(
                machine.aggregator_name, machine.machine_type, machine.machine_id
            )
        elif new_state == MachineState.FREE and old_state == MachineState.DONE:
            database.end_cycle(
                machine.aggregator_name, machine.machine_type, machine.machine_id
            )
            
        # Send notifications
        notification_manager.on_state_change(machine, old_state, new_state)


def offline_check_loop():
    """Periodically check for offline machines"""
    while True:
        time.sleep(60)  # Check every minute
        state_changes = state_machine.check_offline()
        
        for machine, old_state, new_state in state_changes:
            database.store_state_change(
                machine.aggregator_name,
                machine.machine_type,
                machine.machine_id,
                old_state,
                new_state
            )


def cleanup_loop():
    """Periodically clean up old database records"""
    while True:
        time.sleep(86400)  # Once per day
        database.cleanup_old_data(days=30)


# ============================================================================
# Main
# ============================================================================

def main():
    global state_machine, database, notification_manager, config
    
    parser = argparse.ArgumentParser(description='Washing Machine Monitoring Server')
    parser.add_argument('--config', default='config.json', help='Config file path')
    parser.add_argument('--mock', action='store_true', help='Use mock data generation')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()
    
    # Load configuration
    config_path = Path(__file__).parent / args.config
    config = load_config(config_path)
    
    # Override config with environment variables if set
    if os.getenv('WEB_HOST'):
        config['web_host'] = os.getenv('WEB_HOST')
    if os.getenv('WEB_PORT'):
        config['web_port'] = int(os.getenv('WEB_PORT'))
    if os.getenv('DATABASE_PATH'):
        config['database_path'] = os.getenv('DATABASE_PATH')
    
    logger.info("=" * 60)
    logger.info("Washing Machine Monitoring Server (HTTP Mode)")
    logger.info("=" * 60)
    
    # Initialize components
    thresholds = Thresholds(
        running_rms=config['thresholds']['running_rms'],
        done_minutes=config['thresholds']['done_minutes'],
        free_minutes=config['thresholds']['free_minutes']
    )
    
    database = Database(config.get('database_path', 'washing_machines.db'))
    state_machine = StateMachine(thresholds, config)
    state_machine.load_assignments(database.get_assignments())
    notification_manager = NotificationManager(config)
    
    # Start background threads
    offline_thread = threading.Thread(target=offline_check_loop, daemon=True)
    offline_thread.start()
    
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    
    # Start mock data if requested
    if args.mock:
        logger.info("Starting mock data generation")
        from lora_receiver import MockLoRaReceiver
        mock_receiver = MockLoRaReceiver()
        mock_receiver.set_callback(on_reading_received)
        mock_receiver.start()
    
    # Start Flask app
    logger.info(f"Starting web server on {config['web_host']}:{config['web_port']}")
    logger.info(f"LoRa data endpoint: http://{config['web_host']}:{config['web_port']}/api/lora-data")
    app.run(
        host=config.get('web_host', '0.0.0.0'),
        port=config.get('web_port', 8080),
        debug=args.debug,
        use_reloader=False  # Disable reloader to prevent double initialization
    )


if __name__ == '__main__':
    main()
