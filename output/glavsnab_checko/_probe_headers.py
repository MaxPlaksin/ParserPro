import os

import paramiko

PROBE = r"""
python3 - << 'PY'
import urllib.request
url = "https://checko.ru/search?query=7703430805"
headers = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36","Accept-Language":"ru"}
req = urllib.request.Request(url, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=25) as r:
        print("simple", getattr(r, "status", None), r.geturl())
except Exception as e:
    print("simple", getattr(e, "code", type(e).__name__), str(e)[:120])
PY
"""

host = "185.200.241.148"
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    host,
    username="root",
    password=os.environ["SSH_PASS_185_200_241_148"],
    timeout=25,
    banner_timeout=40,
    auth_timeout=25,
    allow_agent=False,
    look_for_keys=False,
)
_stdin, stdout, stderr = client.exec_command(PROBE, timeout=40)
print(stdout.read().decode("utf-8", "replace"))
print(stderr.read().decode("utf-8", "replace")[:300])
client.close()
