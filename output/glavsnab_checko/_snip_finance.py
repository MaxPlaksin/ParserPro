import os

import paramiko

REMOTE = r"""
python3 - << 'PY'
import urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
req = urllib.request.Request(
    "https://checko.ru/company/fasad-stroy-1177746441278",
    headers={"User-Agent": UA, "Accept-Language": "ru"},
)
with urllib.request.urlopen(req, timeout=25) as response:
    body = response.read().decode("utf-8", "replace")
open("/tmp/card.html","w",encoding="utf-8").write(body)
idx = body.find("2110")
print("--- 2110 ---")
print(body[max(0, idx-500):idx+800].replace("\n"," "))
print("--- VYR ---")
idx = body.find("Выручка")
print(body[max(0, idx-200):idx+500].replace("\n"," "))
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
