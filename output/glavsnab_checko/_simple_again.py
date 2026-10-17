import os

import paramiko

REMOTE = r"""
python3 - << 'PY'
import urllib.request
url = "https://checko.ru/search?query=552101436875"
headers_sets = [
    {"User-Agent": "Mozilla/5.0", "Accept-Language": "ru"},
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36", "Accept-Language": "ru"},
]
for headers in headers_sets:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            print("OK", headers["User-Agent"][:20], getattr(response, "status", None), response.geturl()[:80])
    except Exception as exc:
        print("FAIL", headers["User-Agent"][:20], getattr(exc, "code", type(exc).__name__))
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
client.close()
