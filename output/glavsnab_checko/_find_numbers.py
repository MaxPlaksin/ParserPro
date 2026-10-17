import os

import paramiko

REMOTE = r"""
python3 - << 'PY'
body = open("/tmp/card.html", encoding="utf-8").read()
for token in ("chart-2110", "series", "highcharts", "Chartist", "labels:", "data-series", "fsData", "finData", "revenue"):
    print(token, body.lower().find(token.lower()))
idx = body.find("id=\"chart-2110\"")
print("CHART CONTEXT")
print(body[idx:idx+2500])
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
text = stdout.read().decode("utf-8", "replace")
print(text[:4000])
client.close()
