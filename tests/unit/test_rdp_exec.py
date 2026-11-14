from samurai_ai.execution.remote import RdpExecTool, rdp_exec_command


def test_rdp_exec_uses_native_driver_and_secret_env() -> None:
    tool = RdpExecTool()
    plain, secret = tool.build_env({"password": "secret-value"})

    assert tool.force_native is True
    assert tool.definition.id == "remote.rdp-exec"
    assert "password" not in plain.values()
    assert secret["SAMURAI_RDP_PASS"] == "secret-value"


def test_rdp_exec_command_redirects_output_and_exit_code() -> None:
    command = rdp_exec_command("id; whoami", "run-1")

    assert command.startswith("sh -c '(")
    assert "~/thinclient_drives/samurai/run-1.out" in command
    assert "~/thinclient_drives/samurai/run-1.code" in command
    assert "2>&1" in command
    assert "; printf" in command
