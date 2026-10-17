import os

import paramiko

HOSTS = ("194.87.226.99", "185.200.241.148")
PROBE = r"""
python3 - << 'PY'
import urllib.request
url = "https://checko.ru/search?query=5032340305"
req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36","Accept-Language":"ru"})
try:
    with urllib.request.urlopen(req, timeout=25) as r:
        body = r.read(240).decode("utf-8","replace").replace("\n"," ")
        print("status", getattr(r, "status", None))
        print("url", r.geturl())
        print("body", body[:160])
except Exception as e:
    if hasattr(e, "code"):
        raw = e.read(180).decode("utf-8","replace").replace("\n"," ")
        print("http", e.code, raw[:160])
    else:
        print(type(e).__name__, e)
PY
"""

for host in HOSTS:
    print("====", host)
    password = os.environ["SSH_PASS_" + host.replace(".", "_")]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username="root",
        password=password,
        timeout=25,
        banner_timeout=40,
        auth_timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    _stdin, stdout, stderr = client.exec_command(PROBE, timeout=40)
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print(err[:300])
    client.close()
