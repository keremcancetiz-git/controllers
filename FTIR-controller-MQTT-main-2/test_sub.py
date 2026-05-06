import paho.mqtt.client as mqtt

BROKER = "192.168.1.100"
PORT = 1883
TOPIC = "master/bypass/FTIR/#"   # # = wildcard, catches everything under FTIR

def on_connect(client, userdata, flags, rc, properties=None):
    print(f"Connected (rc={rc})")
    client.subscribe(TOPIC)
    print(f"Subscribed: {TOPIC}")

def on_message(client, userdata, msg):
    print(f"← {msg.topic}: {msg.payload.decode(errors='replace')}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT, 60)
client.loop_forever()