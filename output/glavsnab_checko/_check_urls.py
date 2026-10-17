import os

import paramiko

REMOTE = r"""
python3 - << 'PY'
import urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
urls = [
    "https://checko.ru/search?query=7704409716",
    "https://checko.ru/company/1225000039131/finances/accounting",
    "https://checko.ru/search?query=552101436875",
]
for url in urls:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            body = response.read()
            print("OK", getattr(response, "status", None), len(body), response.geturl()[:90])
    except Exception as exc:
        print("FAIL", getattr(exc, "code", type(exc).__name__), url[:70])
PY
"""

for host in ("185.200.241.148", "194.87.226.99"):
    print("====", host)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username="root",
        password=os.environ["SSH_PASS_" + host.replace(".", "_")],
        timeout=25,
        banner_timeout=40,
        auth_timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    _stdin, stdout, _stderr = client.exec_command("pkill -f remote_checko_run.py || true; echo stopped", timeout=15)
    print(stdout.read().decode("utf-8", "replace"))
    _stdin, stdout, stderr = client.exec_command(REMOTE, timeout=60)
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print(err[:200])
    client.close()
