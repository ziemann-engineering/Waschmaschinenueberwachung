"""
Washing Machine Monitoring Server
Main entry point
"""

import json
import os
import time
import threading
import argparse
import logging
from pathlib import Path

from flask import Flask, render_template, jsonify, request, abort

from lora_receiver import MachineReading
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


@app.route('/')
def index():
    """Main page - all aggregators"""
    status = state_machine.get_all_status()
    return render_template('index.html', aggregators=status, config=config)


@app.route('/info')
def info_page():
    """Info page about the project"""
    return render_template('info.html')


@app.route('/<name>')
def aggregator_page(name: str):
    """Single aggregator page by name (e.g., /G13, /D2)"""
    # Find aggregator by name
    aggregator_id = None
    for agg_id, agg_config in config.get("aggregators", {}).items():
        if agg_config.get("name") == name:
            aggregator_id = int(agg_id)
            break
    
    if aggregator_id is None:
        abort(404)
    
    status = state_machine.get_aggregator_status(aggregator_id)
    if not status:
        abort(404)
    return render_template('aggregator.html', aggregator=status, config=config)


@app.route('/api/status')
def api_status():
    """API endpoint - all status data"""
    return jsonify(state_machine.get_all_status())


@app.route('/api/aggregator/<int:aggregator_id>')
def api_aggregator(aggregator_id: int):
    """API endpoint - single aggregator status"""
    status = state_machine.get_aggregator_status(aggregator_id)
    if not status:
        abort(404)
    return jsonify(status)


@app.route('/api/machine/<int:aggregator_id>/<int:machine_id>')
def api_machine(aggregator_id: int, machine_id: int):
    """API endpoint - single machine status"""
    status = state_machine.get_machine_status(aggregator_id, machine_id)
    if not status:
        abort(404)
    return jsonify(status)


@app.route('/api/history/<int:aggregator_id>/<int:machine_id>')
def api_history(aggregator_id: int, machine_id: int):
    """API endpoint - machine reading history"""
    hours = request.args.get('hours', 24, type=float)
    readings = database.get_recent_readings(aggregator_id, machine_id, hours)
    cycles = database.get_cycle_history(aggregator_id, machine_id, 20)
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
        watch_aggregator=data.get('aggregator_id'),
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
    try:
        # Expect JSON with hex encoded packet data
        data = request.json
        if not data or 'packet_data' not in data:
            return jsonify({'error': 'Missing packet_data'}), 400
        
        # Decode the hex packet data
        try:
            packet_data = bytes.fromhex(data['packet_data'])
        except Exception as e:
            return jsonify({'error': f'Invalid hex packet data: {e}'}), 400
        
        # Parse the packet (same logic as LoRa receiver)
        if len(packet_data) < 2:
            return jsonify({'error': 'Packet too short'}), 400
            
        aggregator_id = packet_data[0]
        machine_count = packet_data[1]
        
        logger.info(f"Received HTTP LoRa packet: aggregator={aggregator_id}, machines={machine_count}")
        
        # Handle heartbeat packets (0 machines)
        if machine_count == 0:
            logger.info(f"Received heartbeat from aggregator {aggregator_id}")
            return jsonify({'success': True, 'type': 'heartbeat'})
        
        # Parse machine data
        if len(packet_data) < 2 + (machine_count * 7):
            return jsonify({'error': 'Packet too short for machine data'}), 400
            
        import struct
        import time
        
        timestamp = time.time()
        offset = 2
        readings_processed = 0
        
        for i in range(machine_count):
            if offset + 7 > len(packet_data):
                break
                
            machine_type = packet_data[offset]
            machine_id = packet_data[offset+1]
            rms_x100 = struct.unpack('<H', packet_data[offset+2:offset+4])[0]
            freq_x10 = struct.unpack('<H', packet_data[offset+4:offset+6])[0]
            battery = packet_data[offset+6]
            
            from lora_receiver import MachineReading
            reading = MachineReading(
                aggregator_id=aggregator_id,
                machine_type=machine_type,
                machine_id=machine_id,
                rms=rms_x100 / 100.0,
                dominant_freq=freq_x10 / 10.0,
                battery_percent=battery,
                timestamp=timestamp
            )
            
            # Process the reading (same as LoRa callback)
            on_reading_received(reading)
            
            type_str = "W" if machine_type == 1 else "T"
            logger.info(
                f"[{type_str}] Machine {aggregator_id}/{machine_id}: "
                f"RMS={reading.rms:.2f} m/s², "
                f"Freq={reading.dominant_freq:.1f} Hz, "
                f"Batt={reading.battery_percent}%"
            )
            
            offset += 7
            readings_processed += 1
            
        return jsonify({
            'success': True, 
            'type': 'data',
            'readings_processed': readings_processed
        })
        
    except Exception as e:
        logger.exception(f"Error processing LoRa data: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Data Processing
# ============================================================================

def on_reading_received(reading: MachineReading):
    """Callback when a sensor reading is received"""
    # Store in database
    database.store_reading(reading)
    
    # Update state machine
    state_change = state_machine.update(reading)
    
    # Handle state changes
    if state_change:
        machine, old_state, new_state = state_change
        
        # Store state change
        database.store_state_change(
            machine.aggregator_id,
            machine.machine_id,
            old_state,
            new_state
        )
        
        # Track cycles
        if new_state == MachineState.RUNNING and old_state in (
            MachineState.FREE, MachineState.DONE, MachineState.UNKNOWN
        ):
            database.start_cycle(machine.aggregator_id, machine.machine_id)
        elif new_state == MachineState.FREE and old_state == MachineState.DONE:
            database.end_cycle(machine.aggregator_id, machine.machine_id)
            
        # Send notifications
        notification_manager.on_state_change(machine, old_state, new_state)


def offline_check_loop():
    """Periodically check for offline machines"""
    while True:
        time.sleep(60)  # Check every minute
        state_changes = state_machine.check_offline()
        
        for machine, old_state, new_state in state_changes:
            database.store_state_change(
                machine.aggregator_id,
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
    
    state_machine = StateMachine(thresholds, config)
    database = Database(config.get('database_path', 'washing_machines.db'))
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
