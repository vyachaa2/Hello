import os
import sys
import subprocess

room_id = os.environ.get("HIGHRISE_ROOM_ID", "")
api_token = os.environ.get("HIGHRISE_API_TOKEN", "")

if not room_id or not api_token:
    print("ERROR: HIGHRISE_ROOM_ID and HIGHRISE_API_TOKEN secrets must be set.")
    print("Please add them in the Secrets tab (lock icon) in your Replit sidebar.")
    sys.exit(1)

cmd = ["highrise", "bot:Bot", room_id, api_token]
print(f"Starting Highrise bot in room: {room_id}")
result = subprocess.run(cmd)
sys.exit(result.returncode)
