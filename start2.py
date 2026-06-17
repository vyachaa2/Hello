import os
import sys
import subprocess

room_id = os.environ.get("HIGHRISE_ROOM_ID_2", "")
api_token = os.environ.get("HIGHRISE_API_TOKEN_2", "")

if not room_id or not api_token:
    print("ERROR: HIGHRISE_ROOM_ID_2 and HIGHRISE_API_TOKEN_2 must be set.")
    sys.exit(1)

cmd = ["highrise", "bot2:Bot2", room_id, api_token]
print(f"[BOT2] Запуск в комнате: {room_id}")
result = subprocess.run(cmd)
sys.exit(result.returncode)
