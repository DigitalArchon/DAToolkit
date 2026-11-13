import os
import stat

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


def test_private_dir_makes_every_level_private(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    os.chmod(share, 0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(share))
    loose = config.data_dir()
    loose.mkdir()
    os.chmod(loose, 0o755)                                   # as an earlier version left it
    case_dir = config.private_dir(config.data_dir() / "cases" / "20260101-000000-x")
    for d in (config.data_dir(), config.data_dir() / "cases", case_dir):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    assert stat.S_IMODE(share.stat().st_mode) == 0o755    # above DA Toolkit's root: not ours
    # outside both roots (a case root given by hand): private from that directory down
    other = config.private_dir(tmp_path / "elsewhere" / "c1")
    assert stat.S_IMODE(other.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "elsewhere").stat().st_mode) != 0o700
    # at start, what earlier versions made loosely is tightened; links are left alone
    (config.data_dir() / "baselines").mkdir()
    os.chmod(config.data_dir() / "baselines", 0o755)
    (config.data_dir() / "linked").symlink_to(tmp_path / "elsewhere")
    config.tighten_dirs()
    assert stat.S_IMODE((config.data_dir() / "baselines").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "elsewhere").stat().st_mode) != 0o700


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
    for local in ("http://10.0.0.5:8000/v1", "http://172.20.1.1/v1", "http://[fd00::5]:11434/v1",
                  "http://[fe80::1%25eth0]:11434/v1", "http://169.254.10.10/v1", "http://[::ffff:192.168.1.9]/v1",
                  "http://127.0.0.1:1234/v1", "http://[::1]/v1", "https://gpu.lan/v1", "http://box.local/v1"):
        assert is_local_url(local), local
    # public addresses Python's is_private counts as private (6to4 of 8.8.8.8, Teredo), and others
    for public in ("https://[2002:808:808::1]/v1", "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/v1",
                   "https://[64:ff9b:1::808:808]/v1", "https://198.18.0.1/v1", "https://[::ffff:8.8.8.8]/v1",
                   "https://100.64.0.1/v1"):
        assert not is_local_url(public), public
        assert detect_tier("llama3", public) == "standard"


def test_ssh_argv():
    h = Host("r1", "ssh", "10.1.1.1", port=2222, user="admin", auth="password", jump="me@bastion",
             ssh_options=["KexAlgorithms=+diffie-hellman-group14-sha1", " "])
    argv = ssh_argv(h)
    assert argv[0] == "ssh" and argv[-2:] == ["--", "10.1.1.1"]          # never read as an option
    assert ["-p", "2222"] == argv[argv.index("-p"):argv.index("-p") + 2]
    assert "-J" in argv and "me@bastion" in argv
    assert "PubkeyAuthentication=no" in argv
    assert argv.count("-o") == 4
    k = ssh_argv(Host("s", "ssh", "srv", auth="key", key_file="~/.ssh/id"))
    assert "-i" in k and "IdentitiesOnly=yes" in k


def test_old_guacd_address_keys_are_ignored():
    from datoolkit.sessions import guac

    cfg = config.from_dict({"settings": {"guacd_host": "10.0.0.5", "guacd_port": 9999, "font_size": 14}})
    assert cfg.settings.font_size == 14
    assert "guacd_host" not in cfg.to_dict()["settings"]           # dropped on the next save
    assert guac.GUACD == ("127.0.0.1", 4822)                       # always this machine


def test_reasoning_effort_defaults_to_medium_and_untouched_old_defaults_move_with_it():
    from datoolkit.config import Settings, from_dict
    assert Settings().generation == {"temperature": 0.3, "reasoning_effort": "medium"}
    old = from_dict({"settings": {"generation": {"temperature": 0.3, "reasoning_effort": "low"}}})
    assert old.settings.generation["reasoning_effort"] == "medium"
    chosen = from_dict({"settings": {"generation": {"temperature": 0.5, "reasoning_effort": "low"}}})
    assert chosen.settings.generation == {"temperature": 0.5, "reasoning_effort": "low"}      # the technician's choice
    high = from_dict({"settings": {"generation": {"temperature": 0.3, "reasoning_effort": "high"}}})
    assert high.settings.generation["reasoning_effort"] == "high"


def test_choosing_low_effort_after_the_move_stays_chosen():
    # the move to the new defaults ran on every load, so "low" (with the default temperature)
    # chosen in Settings went back to medium at each launch
    from datoolkit.config import Settings, from_dict
    s = Settings()
    s.generation = {"temperature": 0.3, "reasoning_effort": "low"}
    saved = {"settings": _round_trip(s)}
    assert from_dict(saved).settings.generation == {"temperature": 0.3, "reasoning_effort": "low"}


def _round_trip(settings) -> dict:
    import tomllib
    from dataclasses import asdict

    import tomli_w
    return tomllib.loads(tomli_w.dumps(asdict(settings)))


def test_a_config_that_cant_be_read_is_set_aside_not_a_crash(tmp_path):
    from datoolkit.config import load_or_set_aside, save
    path = tmp_path / "config.toml"
    path.write_text('[[providers]]\nname = "NanoGPT"\nbase_url = "https://nano-gpt.com/api/v1"\n[settings\n')   # a typo
    cfg, notice = load_or_set_aside(path)
    assert cfg.providers == [] and "couldn't be read" in notice
    kept = next(tmp_path.glob("config.toml.broken-*"))
    assert 'name = "NanoGPT"' in kept.read_text()
    save(cfg, path)                                              # the next save writes a new file beside it
    assert 'name = "NanoGPT"' in kept.read_text() and path.exists()
    import stat
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not list(tmp_path.glob(".config.toml.*.tmp"))
    assert load_or_set_aside(path)[1] == ""


def test_a_damaged_case_json_doesnt_break_the_case_list(tmp_path):
    import json

    from datoolkit.case import Case
    good = Case.create("good", "open", root=tmp_path)
    nameless = Case.create("nameless", "confidential", root=tmp_path)
    (nameless.dir / "case.json").write_text(json.dumps({"notes": "x"}))
    listy = Case.create("listy", "open", root=tmp_path)
    (listy.dir / "case.json").write_text("[1, 2]")
    found = {c["id"]: c for c in Case.list_all(root=tmp_path)}
    assert found[good.id]["name"] == "good"
    assert found[nameless.id]["name"] == nameless.id and found[nameless.id]["sensitivity"] == "sovereign"
    assert Case.load(nameless.id, root=tmp_path).sensitivity == "sovereign"
    assert found[listy.id]["name"] == "listy"                   # from the audit log's first event instead


def test_a_save_never_writes_over_what_another_instance_saved(tmp_path):
    """Two DA Toolkits each read config.toml when they started. The one whose copy is older
    than the file writes nothing: until Nov 2026 it wrote its whole copy, and a host saved in
    the other was gone."""
    import pytest
    path = tmp_path / "config.toml"
    config.save(Config(hosts=[Host("old", "ssh", "10.0.0.1")]), path)
    a, b = config.load(path), config.load(path)
    a.hosts.append(Host("web01", "ssh", "10.0.0.2"))
    config.save(a, path)
    assert config.changed_elsewhere(b) and not config.changed_elsewhere(a)
    b.settings.layout = {"chat_w": 500}
    with pytest.raises(config.ChangedElsewhere):
        config.save(b, path)
    assert [h.name for h in config.load(path).hosts] == ["old", "web01"]
    a.clients.append("Acme")                                    # the one that is current goes on saving
    config.save(a, path)
    assert config.load(path).clients == ["Acme"] and not list(tmp_path.glob(".config.toml.*.tmp"))
    with pytest.raises(config.ChangedElsewhere):                # and the other stays refused, however often
        config.save(b, path)


def test_a_first_start_and_a_file_set_aside_still_save(tmp_path):
    import pytest
    path = tmp_path / "config.toml"
    first, other = config.load(path), config.load(path)         # two first starts: no file yet
    first.clients.append("Acme")
    config.save(first, path)
    with pytest.raises(config.ChangedElsewhere):                # there is a file now, and it isn't the other's
        config.save(other, path)
    path.write_text("[settings\n")
    cfg, notice = config.load_or_set_aside(path)
    assert notice
    cfg.clients.append("Bolt")
    config.save(cfg, path)
    config.save(cfg, path)
    assert config.load(path).clients == ["Bolt"]


def test_an_empty_config_beside_cases_is_set_aside_like_a_broken_one():
    from datoolkit.case import Case
    path = config.config_dir() / "config.toml"
    config.private_dir(path.parent)
    path.write_text(" \n")
    assert config.load_or_set_aside() == (Config(), "")         # nothing here yet: a first start, no settings
    Case.create("a ticket", "open")
    cfg, notice = config.load_or_set_aside()
    assert cfg == Config() and "it is empty" in notice and "config.toml.broken-" in notice
    assert [p.read_text() for p in path.parent.glob("config.toml.broken-*")] == [" \n"] and not path.exists()
    config.save(cfg, path)                                      # and the settings made next are saved
    assert config.load_or_set_aside()[1] == ""


def test_a_file_is_on_the_disk_before_it_takes_its_name(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    flushed, real_fsync, real_replace = [], os.fsync, os.replace

    def fsync(fd):
        flushed.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else path.read_bytes())
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    config.write_atomic(path, b"new")
    assert flushed == [b"old", "dir"] and path.read_bytes() == b"new"      # flushed while the old file had the name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def no_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", no_replace)
    with pytest.raises(OSError):
        config.write_atomic(path, b"never")
    monkeypatch.setattr(os, "replace", real_replace)
    assert path.read_bytes() == b"new" and [p.name for p in tmp_path.iterdir()] == ["state.json"]   # no temp file left
