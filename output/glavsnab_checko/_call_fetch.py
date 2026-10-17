import os

import paramiko

REMOTE = r"""
cd /root/glavsnab-checko && python3 - << 'PY'
import urllib.request
from remote_checko_run import fetch
opener = urllib.request.build_opener()
try:
    html, url = fetch(opener, "https://checko.ru/search?query=552101436875")
    print("OK", url, len(html))
except Exception as exc:
    print("FAIL", getattr(exc, "code", ""), getattr(exc, "url", ""), exc)
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
_stdin, stdout, stderr = client.exec_command(REMOTE, timeout=40)
print(stdout.read().decode("utf-8", "replace"))
print(stderr.read().decode("utf-8", "replace")[:500])
client.close()
