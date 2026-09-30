import pytest

from datoolkit.safety.redact import redact
from datoolkit.safety.risk import classify, effective
from datoolkit.safety.truncate import head_tail


@pytest.mark.parametrize("cmd", [
    "df -h", "journalctl -u nginx --no-pager -n 200", "last reboot", "systemctl status sshd --no-pager",
    "ip addr show", "Get-Service | Where-Object {$_.Status -eq 'Stopped'}", "show running-config",
    "cat /var/log/syslog 2>/dev/null | tail -50", "ls -la > /dev/null", "grep -r shutdown /var/log/syslog",
    "Get-WinEvent -LogName System -MaxEvents 50 | Format-List", "ping -c 4 8.8.8.8", "Import-Module ActiveDirectory",
    "cat /etc/passwd", "getent passwd 1000 999",
    "sudo ls -la /opt/seafile-mysql /opt/seafile-data/ssl 2>&1 | head -40; getent passwd 1000 999",
])
def test_read_only(cmd):
    assert classify(cmd) == ("read_only", [])


@pytest.mark.parametrize("cmd", [
    "sudo apt install htop", "sed -i 's/a/b/' /etc/x.conf", "echo hi > /etc/motd", "sudo systemctl start nginx",
    "Set-Service -Name Spooler -StartupType Manual", "configure terminal", "write memory", "clear counters",
    "mkdir /tmp/x", "docker restart web", "ipconfig /flushdns",
    "sudo passwd root", "echo x | passwd --stdin bob", "/usr/bin/passwd -l bob",
])
def test_modifying(cmd):
    assert classify(cmd)[0] == "modifying"


@pytest.mark.parametrize("cmd", [
    "rm -rf /var/lib/foo", "sudo reboot", "shutdown -h now", "systemctl restart nginx", "mkfs.ext4 /dev/sdb1",
    "dd if=/dev/zero of=/dev/sda", "Restart-Computer -Force", "Stop-Service Spooler", "reload",
    "iptables -F", "Remove-Item C:\\temp -Recurse", "/system reboot", "diskpart", "kill -9 1234",
    "ls; sudo shutdown -r now",
])
def test_disruptive(cmd):
    assert classify(cmd)[0] == "disruptive"


def test_effective_only_raises():
    assert effective("disruptive", "df -h")[0] == "disruptive"
    assert effective("read_only", "sudo reboot")[0] == "disruptive"
    assert effective("nonsense", "df -h")[0] == "modifying"


@pytest.mark.parametrize("text, secret", [
    ("DB_PASSWORD=hunter2", "hunter2"),
    ("password: 'correct horse'", "correct horse"),
    ("api_key = sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz"),
    ("Authorization: Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
    ("root:$6$saltsalt$abcdefghijklmnopqrstuv:19000:0:99999:7:::", "abcdefghijklmnopqrstuv"),
    ("enable secret 5 $1$abcd$efghijklmnop", "efghijklmnop"),
    ("username admin privilege 15 password 7 0822455D0A16", "0822455D0A16"),
    (" password s3cr3t!", "s3cr3t!"),
    ("snmp-server community Publ1cStr RO", "Publ1cStr"),
    ("crypto isakmp key MyPsk123 address 1.2.3.4", "MyPsk123"),
    ("New-LocalUser bob -Password 'P@ssw0rd'", "P@ssw0rd"),
    ("https://admin:topsecret@example.com/x", "topsecret"),
    ("AWS key AKIAABCDEFGHIJKLMNOP here", "AKIAABCDEFGHIJKLMNOP"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----", "abc"),
    ("/interface wireless security-profiles set wpa2-pre-shared-key=Sup3rS3cret", "Sup3rS3cret"),
])
def test_redacts(text, secret):
    out, n = redact(text)
    assert secret not in out
    assert n >= 1


@pytest.mark.parametrize("text", [
    "Failed password for invalid user admin from 10.0.0.5 port 50022 ssh2",
    "Accepted password for bob from 10.0.0.9 port 51234 ssh2",
    "PasswordAuthentication yes",
    "[sudo] password for bob:",
    "Filesystem      Size  Used Avail Use% Mounted on",
])
def test_leaves_normal_output(text):
    assert redact(text) == (text, 0)


def test_head_tail():
    text = "\n".join(f"line {i}" for i in range(1000))
    out, cut = head_tail(text, 30, 100000)
    assert cut
    assert out.startswith("line 0\n")
    assert out.endswith("line 999")
    assert "970 lines omitted" in out
    assert head_tail("short", 30, 1000) == ("short", False)
    out, cut = head_tail("x" * 5000, 30, 300)
    assert cut and len(out) < 400
