import os

import paramiko


for host in ("194.87.226.99", "185.200.241.148"):
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
    _stdin, stdout, _stderr = client.exec_command(
        "ps -eo pid,args | grep remote_checko_run.py | grep -v grep || echo no-runner",
        timeout=20,
    )
    print("====", host)
    print(stdout.read().decode("utf-8", "replace").strip())
    client.close()
