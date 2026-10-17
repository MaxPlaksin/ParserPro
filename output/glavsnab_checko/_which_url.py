import os

import paramiko

REMOTE = r"""
python3 - << 'PY'
import urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
urls = [
    "https://checko.ru/search?query=7707083893",
    "https://checko.ru/search/tips?query=7707083893",
    "https://checko.ru/company/sberbank-1027700132195",
]
for url in urls:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            print("OK", getattr(response, "status", None), len(response.read()), response.geturl()[:90])
    except Exception as exc:
        print("FAIL", getattr(exc, "code", type(exc).__name__), url)
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
_stdin, stdout, stderr = client.exec_command(REMOTE, timeout=50)
print(stdout.read().decode("utf-8", "replace"))
client.close()
