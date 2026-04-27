"""
FTIR MQTT controller.

Subscribes to four command topics and publishes responses on the matching
`/response` topic. Long-running OMNIC operations run in a worker thread
so MQTT stays responsive; a global busy flag rejects overlapping
collect-sample / collect-bkg requests with a `busy` response.

Topic layout  (base_topic configured in config.json)
------------
  <base_topic>/collect-bkg          -> {"experiment_name": "...", "experiment_name_suffix": "..."}  (request)
  <base_topic>/collect-bkg/response -> {"status": ..., ...}                                           (response)

  <base_topic>/collect-sample           -> {"experiment_name": "...", "experiment_name_suffix": "..."}
  <base_topic>/collect-sample/response  -> {"status": ..., ...}

  experiment_name_suffix is optional; when given it is appended to the exported filename:
    bkg:    bkg__{timestamp}_{suffix}.spa/.csv
    sample: {timestamp}_{suffix}.spa/.csv

  <base_topic>/experiment-folders          -> {}                            (no payload needed)
  <base_topic>/experiment-folders/response -> {"status": ..., "folders": [...]}

  <base_topic>/get-absorbance          -> {"experiment_name": "...", "wavenumber": 1234.5}
  <base_topic>/get-absorbance/response -> {"status": ..., "results": [...]}

Response shapes
---------------
  success:  {"status": "success", "function": "...", <extra fields>}
  error:    {"status": "error",   "function": "...", "error": "..."}
  busy:     {"status": "busy",    "function": "..."}
"""
import json
import os
import sys
import time
import threading
import traceback
from datetime import datetime

import numpy as np
import pandas as pd
import paho.mqtt.client as mqtt

from omnic import Conversation, Command


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

with open(CONFIG_PATH, 'r') as f:
    CONFIG = json.load(f)

MQTT_BROKER    = CONFIG['mqtt']['broker']
MQTT_PORT      = CONFIG['mqtt']['port']
BASE_TOPIC     = CONFIG['mqtt']['base_topic']

ROOT_FOLDER = os.path.expanduser(CONFIG['paths']['root_folder'])


# ---------------------------------------------------------------------------
# Busy state
# ---------------------------------------------------------------------------

busy_lock = threading.Lock()
busy = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def publish_response(client, command_name: str, payload: dict):
    """Publish a response on `<prefix>/<command>/response`."""
    topic = f"{BASE_TOPIC}/{command_name}/response"
    client.publish(topic, json.dumps(payload))
    print(f"→ {topic}: {payload}")


def pick_absorbance(address: str, wavenumber: float) -> float:
    """Return absorbance at the wavenumber closest to `wavenumber` in the CSV."""
    spectrum = pd.read_csv(
        address,
        names=['Wavenumber', 'Absorbance'],
        dtype={'Wavenumber': 'float64', 'Absorbance': 'float64'},
        na_values='#NaN'
    )
    idx = np.argmin(np.abs(spectrum['Wavenumber'] - wavenumber))
    return float(spectrum.iat[idx, 1])


def wait_for_menu_status(conversation: Conversation, parameter: str,
                         poll_interval: float = 1.0):
    """Block until OMNIC reports the menu item as Enabled (= collection done)."""
    menu_status = 'Disabled'
    while menu_status.strip() != 'Enabled':
        menu_status = conversation.request(parameter)
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# OMNIC handlers (threaded)
# ---------------------------------------------------------------------------

def handle_collect_background(client, payload):
    global busy
    with busy_lock:
        if busy:
            publish_response(client, 'collect-bkg', {
                'status': 'busy',
                'function': 'collectBackground'
            })
            return
        busy = True

    threading.Thread(
        target=_collect_background_worker,
        args=(client, payload),
        daemon=True
    ).start()


def _collect_background_worker(client, payload):
    global busy
    import pythoncom
    pythoncom.CoInitialize()

    conversation = None
    experiment_name = payload.get('experiment_name')
    suffix = payload.get('experiment_name_suffix', '')

    try:
        conversation = Conversation()

        conversation.exec([{'command': Command.CollectBackground}])
        wait_for_menu_status(conversation, 'MenuStatus CollectBackground')

        conversation.exec([
            {'command': Command.Display},
            {'command': Command.DisplayLimits, 'args': '4000 500'}
        ])

        if experiment_name:
            timestamp_str = datetime.now().strftime('%Y_%m_%d-%H_%M_%S')
            experiment_folder = os.path.join(ROOT_FOLDER, "Experiments", experiment_name)
            os.makedirs(experiment_folder, exist_ok=True)

            suffix_part = f"_{suffix}" if suffix else ""
            base_name = f"bkg__{timestamp_str}{suffix_part}"
            export_spa = os.path.join(experiment_folder, f"{base_name}.spa")
            export_csv = os.path.join(experiment_folder, f"{base_name}.csv")

            conversation.exec([{'command': Command.Export, 'args': export_spa}])
            conversation.exec([{'command': Command.Export, 'args': export_csv}])
            conversation.exec([
                {'command': Command.Select, 'args': 'All'},
                {'command': Command.DeleteSpectrum}
            ])

        publish_response(client, 'collect-bkg', {
            'status': 'success',
            'function': 'collectBackground',
            'message': 'Background collection finished'
        })
    except Exception as e:
        traceback.print_exc()
        publish_response(client, 'collect-bkg', {
            'status': 'error',
            'function': 'collectBackground',
            'error': str(e)
        })
    finally:
        if conversation:
            try:
                conversation.destroy()
            except Exception:
                pass
        pythoncom.CoUninitialize()
        with busy_lock:
            busy = False


def handle_collect_sample(client, payload):
    global busy
    with busy_lock:
        if busy:
            publish_response(client, 'collect-sample', {
                'status': 'busy',
                'function': 'collectSample'
            })
            return
        busy = True

    threading.Thread(
        target=_collect_sample_worker,
        args=(client, payload),
        daemon=True
    ).start()


def _collect_sample_worker(client, payload):
    global busy
    import pythoncom
    pythoncom.CoInitialize()

    conversation = None
    experiment_name = payload.get('experiment_name')
    suffix = payload.get('experiment_name_suffix', '')
    try:
        conversation = Conversation()

        conversation.exec([{'command': Command.CollectSample}])
        wait_for_menu_status(conversation, 'MenuStatus CollectSample')

        conversation.exec([
            {'command': Command.Display},
            {'command': Command.DisplayLimits, 'args': '4000 500'}
        ])

        if experiment_name:
            timestamp_str = datetime.now().strftime('%Y_%m_%d-%H_%M_%S')
            experiment_folder = os.path.join(ROOT_FOLDER, "Experiments", experiment_name)
            os.makedirs(experiment_folder, exist_ok=True)

            suffix_part = f"_{suffix}" if suffix else ""
            base_name = f"{timestamp_str}{suffix_part}"
            export_spa = os.path.join(experiment_folder, f"{base_name}.spa")
            export_csv = os.path.join(experiment_folder, f"{base_name}.csv")

            conversation.exec([{'command': Command.Export, 'args': export_spa}])
            conversation.exec([{'command': Command.Export, 'args': export_csv}])
            conversation.exec([
                {'command': Command.Select, 'args': 'All'},
                {'command': Command.DeleteSpectrum}
            ])

        publish_response(client, 'collect-sample', {
            'status': 'success',
            'function': 'collectSample',
            'message': 'Sample collection finished'
        })
    except Exception as e:
        traceback.print_exc()
        publish_response(client, 'collect-sample', {
            'status': 'error',
            'function': 'collectSample',
            'error': str(e)
        })
    finally:
        if conversation:
            try:
                conversation.destroy()
            except Exception:
                pass
        pythoncom.CoUninitialize()
        with busy_lock:
            busy = False


# ---------------------------------------------------------------------------
# Filesystem handlers (synchronous — fast, don't touch OMNIC)
# ---------------------------------------------------------------------------

def handle_experiment_folders(client, payload):
    try:
        folders = []
        experiments_folder = os.path.join(ROOT_FOLDER, "Experiments")
        if os.path.exists(experiments_folder):
            for root, dirs, _ in os.walk(experiments_folder):
                for dir_name in dirs:
                    full_path = os.path.join(root, dir_name)
                    created_at = time.ctime(os.path.getctime(full_path))
                    relative_path = os.path.relpath(full_path, experiments_folder)
                    folders.append([relative_path, created_at])

        publish_response(client, 'experiment-folders', {
            'status': 'success',
            'function': 'experimentFolders',
            'folders': folders
        })
    except Exception as e:
        traceback.print_exc()
        publish_response(client, 'experiment-folders', {
            'status': 'error',
            'function': 'experimentFolders',
            'error': str(e)
        })


def handle_get_absorbance(client, payload):
    experiment_name = payload.get('experiment_name')
    wavenumber = payload.get('wavenumber')

    try:
        experiment_folder = os.path.join(ROOT_FOLDER, "Experiments", experiment_name)
        absorbance_results = []

        csv_files = [
            f for f in os.listdir(experiment_folder)
            if f.endswith('.csv') and not f.startswith('bkg')
        ]

        for csv_file in csv_files:
            csv_path = os.path.join(experiment_folder, csv_file)
            timestamp_str = csv_file.replace('.csv', '')

            try:
                dt = datetime.strptime(timestamp_str, '%Y_%m_%d-%H_%M_%S')
                timestamp_ms = int(dt.timestamp() * 1000)
            except ValueError as e:
                print(f"Bad timestamp on {csv_file}: {e}")
                timestamp_ms = None

            absorbance = pick_absorbance(csv_path, wavenumber)
            absorbance_results.append({
                'timestamp': timestamp_ms,
                'absorbance': absorbance
            })

        print(f"Processed {len(csv_files)} CSV files")

        publish_response(client, 'get-absorbance', {
            'status': 'success',
            'function': 'getAbsorbance',
            'wavenumber': wavenumber,
            'results': absorbance_results
        })
    except Exception as e:
        traceback.print_exc()
        publish_response(client, 'get-absorbance', {
            'status': 'error',
            'function': 'getAbsorbance',
            'error': str(e)
        })


# ---------------------------------------------------------------------------
# MQTT wiring
# ---------------------------------------------------------------------------

HANDLERS = {
    'collect-bkg':         handle_collect_background,
    'collect-sample':      handle_collect_sample,
    'experiment-folders':  handle_experiment_folders,
    'get-absorbance':      handle_get_absorbance,
}


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"Connected to {MQTT_BROKER}:{MQTT_PORT}")
        for command in HANDLERS:
            topic = f"{BASE_TOPIC}/{command}"
            client.subscribe(topic)
            print(f"Subscribed: {topic}")
    else:
        print(f"Connection failed (rc={rc})")


def on_message(client, userdata, msg):
    topic_suffix = msg.topic[len(BASE_TOPIC) + 1:]

    # Ignore our own response topics if the broker echoes them back
    if topic_suffix.endswith('/response'):
        return

    print(f"← {msg.topic}: {msg.payload.decode(errors='replace')}")

    try:
        payload = json.loads(msg.payload.decode()) if msg.payload else {}
    except json.JSONDecodeError:
        print(f"Bad JSON on {msg.topic}, treating as empty payload")
        payload = {}

    handler = HANDLERS.get(topic_suffix)
    if handler:
        handler(client, payload)
    else:
        print(f"No handler for topic: {msg.topic}")


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to {MQTT_BROKER}:{MQTT_PORT} ...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        client.disconnect()


if __name__ == '__main__':
    main()