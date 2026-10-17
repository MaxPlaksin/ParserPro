import os

import paramiko

REMOTE = r"""
python3 - << 'PY'
body = open("/tmp/card.html", encoding="utf-8").read()
idx = body.lower().find("series")
print(body[idx-200:idx+1500])
PY
"""

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    "185.200.241.148",
    username="root",
    password=os.environ["SSH_PASS_185_200_241_148"],
    timeout=25,
    banner_timeout=40,
    auth_timeout=25,
    allow_agent=False,
    look_for_keys=False,
)
_stdin, stdout, stderr = client.exec_command(REMOTE, timeout=20)
print(stdout.read().decode("utf-8", "replace")[:2500])
client.close()
