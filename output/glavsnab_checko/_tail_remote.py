import os
import time

import paramiko

time.sleep(75)
for host in ("194.87.226.99", "185.200.241.148"):
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
    _stdin, stdout, stderr = client.exec_command(
        "tail -n 12 /root/glavsnab-checko/run.log; echo OUT $(ls /root/glavsnab-checko/out | wc -l)",
        timeout=20,
    )
    print(stdout.read().decode("utf-8", "replace"))
    client.close()
