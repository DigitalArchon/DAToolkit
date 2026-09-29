from datoolkit import config, creds
from datoolkit.config import Config, Host, Provider
from datoolkit.llm.client import detect_tier, is_local_url
from datoolkit.sessions.ssh import ssh_argv


def test_config_round_trip(tmp_path):
    cfg = Config(providers=[Provider("NanoGPT", config.NANOGPT_BASE_URL, "anthropic/claude-opus-5.5",
                                     {"z-ai/glm-5.3": "tee"})],
                 hosts=[Host("fs01", "ssh", "10.0.0.5", port=2222, user="admin", auth="password",
                             ssh_options=["KexAlgorithms=+diffie-hellman-group14-sha1"])],
                 active_provider="NanoGPT")
    path = tmp_path / "c.toml"
    config.save(cfg, path)
    assert (path.stat().st_mode & 0o777) == 0o600
    loaded = config.load(path)
    assert loaded == cfg
    text = path.read_text()
    assert "api_key" not in text and "password =" not in text


def test_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('active_model = "x"\nfuture_field = 1\n[settings]\nfont_size = 15\nnew_thing = true\n')
    cfg = config.load(path)
    assert cfg.active_model == "x" and cfg.settings.font_size == 15


def test_creds_keyring(memory_keyring):
    assert creds.backend_error() is None
    creds.set_secret("provider", "NanoGPT", "sk-test")
    assert creds.get_secret("provider", "NanoGPT") == "sk-test"
    assert memory_keyring.store[("datoolkit", "provider:NanoGPT")] == "sk-test"
    creds.delete_secret("provider", "NanoGPT")
    creds.delete_secret("provider", "NanoGPT")  # idempotent
    assert creds.get_secret("provider", "NanoGPT") is None


def test_tiers():
    nano = config.NANOGPT_BASE_URL
    assert detect_tier("anthropic/claude-opus-5.5", nano) == "standard"
    assert detect_tier("TEE/glm-5.3", nano) == "tee"
    assert detect_tier("nano-gpt/TEE/glm-4.7", nano) == "tee"
    assert detect_tier("phala/glm-5.3", nano) == "tee"
    assert detect_tier("z-ai/glm-5.3", nano) == "standard"
    assert detect_tier("z-ai/glm-5.3", nano, {"z-ai/glm-5.3": "tee"}) == "tee"
    assert detect_tier("llama3", "http://localhost:11434/v1") == "local"
    assert detect_tier("qwen", "http://192.168.1.20:8000/v1") == "local"
    assert not is_local_url("https://nano-gpt.com/api/v1")


def test_ssh_argv():
    h = Host("r1", "ssh", "10.1.1.1", port=2222, user="admin", auth="password", jump="me@bastion",
             ssh_options=["KexAlgorithms=+diffie-hellman-group14-sha1", " "])
    argv = ssh_argv(h)
    assert argv[0] == "ssh" and argv[-1] == "10.1.1.1"
    assert ["-p", "2222"] == argv[argv.index("-p"):argv.index("-p") + 2]
    assert "-J" in argv and "me@bastion" in argv
    assert "PubkeyAuthentication=no" in argv
    assert argv.count("-o") == 4
    k = ssh_argv(Host("s", "ssh", "srv", auth="key", key_file="~/.ssh/id"))
    assert "-i" in k and "IdentitiesOnly=yes" in k
