"""/rewind drops turns from the context but keeps a note of what already ran."""

import asyncio
from pathlib import Path

from test_app import _chat_ready_stack

from samurai_ai.agents.context import BRANCH_MARK, build_branch_note, is_operator_turn
from samurai_ai.agents.provider import Message, ToolCall, tool_result
from samurai_ai.tui.app import SamuraiApp


def _turn(app: SamuraiApp, ask: str, command: str) -> None:
    call = ToolCall(id=f"c-{command}", name="shell_exec", arguments={"command": command})
    app._remember(Message(role="user", content=ask))
    app._remember(Message(role="assistant", content="", tool_calls=(call,)))
    app._remember(tool_result(call, f"[tool result shell_exec exit=0]\n{command} out"))
    app._remember(Message(role="assistant", content=f"did {command}"))


def test_operator_turns_are_told_apart_from_harness_notes() -> None:
    assert is_operator_turn(Message(role="user", content="scan the host"))
    assert not is_operator_turn(Message(role="user", content="[tool result x] ok"))
    assert not is_operator_turn(Message(role="user", content="<reviewer-note>x"))
    assert not is_operator_turn(Message(role="assistant", content="scan"))


def test_branch_note_names_the_asks_and_the_calls() -> None:
    call = ToolCall(id="c", name="shell_exec", arguments={"command": "nmap -p- t"})
    note = build_branch_note(
        [
            Message(role="user", content="try a full port scan"),
            Message(role="assistant", content="", tool_calls=(call,)),
            tool_result(call, "[tool result] done"),
        ],
        1,
    )
    assert note.content.startswith(BRANCH_MARK)
    assert "try a full port scan" in note.content
    assert "nmap -p- t" in note.content


def test_rewind_keeps_a_note_and_trims_the_store_exactly(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app._chat_history = [Message(role="system", content="sys")]
            _turn(app, "map the web app", "curl /")
            _turn(app, "brute force the login", "hydra login")
            stored_before = app.chat_history.repository.count(operation.id)

            app._rewind_turns("1")

            contents = [m.content for m in app._chat_history]
            assert "map the web app" in contents
            assert "brute force the login" not in contents
            note = app._chat_history[-1]
            assert note.content.startswith(BRANCH_MARK)
            assert "hydra login" in note.content
            # the turn had 3 stored rows (the pure call turn has none) + the note
            assert app.chat_history.repository.count(operation.id) == stored_before - 3 + 1
            events = [e for e in stack[0].events(operation.id) if e.event_type == "chat.rewind"]
            assert events[-1].payload == {"turns": 1, "dropped": 4}

            app._rewind_turns("5")
            assert app._chat_history[-1] is note
            app._rewind_turns("x")
            assert app._chat_history[-1] is note

    asyncio.run(exercise())


def test_undo_does_not_delete_rows_it_never_stored(tmp_path: Path) -> None:
    stack, operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)):
            app._chat_history = [Message(role="system", content="sys")]
            _turn(app, "first ask", "id")
            kept = app.chat_history.repository.count(operation.id)
            _turn(app, "second ask", "pwd")
            app._undo_last_turn()
            assert app.chat_history.repository.count(operation.id) == kept

    asyncio.run(exercise())
