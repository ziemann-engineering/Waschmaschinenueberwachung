"""
Washing Machine Monitoring Server
Main entry point
"""

import json
import time
import threading
import argparse
import logging
from pathlib import Path

from flask import Flask, render_template, jsonify, request, abort

from lora_receiver import LoRaReceiver, MockLoRaReceiver, MachineReading
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
lora_receiver = None


@app.route('/')
def index():
    """Main page - all aggregators"""
    status = state_machine.get_all_status()
    lora_connected = lora_receiver.is_connected if lora_receiver else False
    return render_template('index.html', aggregators=status, config=config, lora_connected=lora_connected)


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
    lora_connected = lora_receiver.is_connected if lora_receiver else False
    return render_template('aggregator.html', aggregator=status, config=config, lora_connected=lora_connected)


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
    global state_machine, database, notification_manager, config, lora_receiver
    
    parser = argparse.ArgumentParser(description='Washing Machine Monitoring Server')
    parser.add_argument('--config', default='config.json', help='Config file path')
    parser.add_argument('--mock', action='store_true', help='Use mock LoRa receiver')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()
    
    # Load configuration
    config_path = Path(__file__).parent / args.config
    config = load_config(config_path)
    
    logger.info("=" * 60)
    logger.info("Washing Machine Monitoring Server")
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
    
    # Initialize LoRa receiver
    if args.mock:
        logger.info("Using MOCK LoRa receiver")
        lora_receiver = MockLoRaReceiver()
    else:
        logger.info(f"Connecting to LoRa on {config['serial_port']}")
        lora_receiver = LoRaReceiver(
            port=config['serial_port'],
            baud_rate=config.get('serial_baud', 9600)
        )
        
    lora_receiver.set_callback(on_reading_received)
    
    # Start background threads
    offline_thread = threading.Thread(target=offline_check_loop, daemon=True)
    offline_thread.start()
    
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    
    # Start LoRa receiver (continues even if connection fails)
    lora_connected = lora_receiver.start()
    if not lora_connected:
        logger.warning("Server running without LoRa receiver - no data will be received")
    
    # Start Flask app
    logger.info(f"Starting web server on {config['web_host']}:{config['web_port']}")
    app.run(
        host=config.get('web_host', '0.0.0.0'),
        port=config.get('web_port', 8080),
        debug=args.debug,
        use_reloader=False  # Disable reloader to prevent double initialization
    )


if __name__ == '__main__':
    main()
