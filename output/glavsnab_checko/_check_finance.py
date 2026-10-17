import os

import paramiko

REMOTE = r"""
python3 - << 'PY'
import urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
opener = urllib.request.build_opener()
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
    try:
        with opener.open(req, timeout=25) as response:
            body = response.read().decode("utf-8", "replace")
            print("OK", getattr(response, "status", None), len(body), response.geturl()[:100])
            return body
    except Exception as exc:
        print("FAIL", getattr(exc, "code", type(exc).__name__), url[:90])
        return ""
page = get("https://checko.ru/company/fasad-stroy-1177746441278")
for token in ("statementData", "Выручка", "Чистая прибыль", "2110"):
    print("has", token, token in page)
acc = get("https://checko.ru/company/1177746441278/finances/accounting")
print("acc statement", "statementData" in acc)
fin = get("https://checko.ru/company/1177746441278/finances")
print("fin statement", "statementData" in fin)
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
_stdin, stdout, stderr = client.exec_command(REMOTE, timeout=60)
print(stdout.read().decode("utf-8", "replace"))
print(stderr.read().decode("utf-8", "replace")[:300])
client.close()
