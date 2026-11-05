from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from samurai_ai.domain.execution import (
    ApprovalRequiredError,
    OutputTrust,
    PromptInjectionScanner,
    SecretRedactor,
    ToolDefinition,
    ToolDeniedError,
    ToolExecutionError,
    ToolResult,
    ToolRun,
    ToolRunStatus,
    ToolStateError,
)
from samurai_ai.domain.kernel import Approval, ApprovalStatus, ArtifactKind, RiskClass
from samurai_ai.domain.offline import check_tool
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.permission import (
    Decision,
    PermissionRule,
    classify_command,
    decide,
    parse_mode,
)
from samurai_ai.domain.scope_guard import ScopeGuardError, guard
from samurai_ai.domain.vault import SecretNotFoundError, SecretScopeError, secret_references
from samurai_ai.execution.registry import ToolOutput, ToolRegistry, default_registry
from samurai_ai.services.kernel import ApprovalNotFoundError, KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.tool_keys import inject
from samurai_ai.services.traffic import TrafficService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.execution import ToolRunRepository


class ToolRunNotFoundError(LookupError):
    pass


class ToolExecutionService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        recovery: RecoveryService,
        runs: ToolRunRepository,
        registry: ToolRegistry | None = None,
        redactor: SecretRedactor | None = None,
        scanner: PromptInjectionScanner | None = None,
        vault: VaultService | None = None,
        traffic: TrafficService | None = None,
        permission_rules: Callable[[], tuple[PermissionRule, ...]] | None = None,
        permission_mode: Callable[[], str | None] | None = None,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.recovery = recovery
        self.runs = runs
        self.registry = registry or default_registry()
        self.redactor = redactor or SecretRedactor()
        self.scanner = scanner or PromptInjectionScanner()
        self.vault = vault
        self.traffic = traffic
        #: the operator's rule book at the boundary: every path (CLI, TUI,
        #: executor, daemon) obeys it, not just the ones that remembered to ask
        self.permission_rules = permission_rules
        self.permission_mode = permission_mode

    def list_tools(self) -> list[ToolDefinition]:
        return self.registry.definitions()

    def get_run(self, run_id: str) -> ToolRun:
        run = self.runs.get_run(run_id)
        if run is None:
            raise ToolRunNotFoundError(f"tool run not found: {run_id}")
        return run

    def list_runs(
        self, operation_id: str, status: ToolRunStatus | None = None
    ) -> list[ToolRun]:
        self.operations.get(operation_id)
        return self.runs.list_runs(operation_id, status)

    def reconcile_runs(self, operation_id: str, actor: str = "operator") -> list[str]:
        """Mark queued/running runs as failed after a crash; never retried."""
        self.operations.get(operation_id)
        return self.runs.mark_in_flight_failed(operation_id, actor)

    def run_tool(
        self,
        operation_id: str,
        tool_id: str,
        params: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
        approval_id: str | None = None,
        tool_version: int | None = None,
        approval_resolver: Callable[[str], bool] | None = None,
    ) -> ToolResult:
        operation = self.operations.get(operation_id)
        adapter = self.registry.get(tool_id, tool_version)
        definition = adapter.definition
        if definition.risk_class is RiskClass.R4:
            raise ToolExecutionError(
                f"tool {definition.id} is risk class r4 and cannot be executed"
            )
        check_tool(definition, operation.offline)
        self._apply_relay(operation, adapter)
        validated = self.registry.validate_params(definition, params or {})
        # the rule book answers before anything is prepared: a denied call must
        # not fail on a missing image first
        decision = self.rule_decision(definition, validated)
        if decision is not None and decision.is_denied:
            rule = decision.rule.format() if decision.rule is not None else "deny"
            raise ToolDeniedError(f"{definition.id} denied by rule {rule}")
        resolved = self._resolve_params(
            validated, operation_id, optional=_optional_params(definition)
        )
        preflight = getattr(adapter, "preflight", None)
        if callable(preflight):
            preflight(resolved)
        # a key the operator stored once beats a parameter the model had to
        # remember: any env name the profile declares is filled from the vault
        # unless the call already carried it. Register the values before the
        # parameters are recorded, so evidence holds names, never plaintext.
        build_env = getattr(adapter, "build_env", None)
        provided: dict[str, str] = {}
        if callable(build_env):
            _, provided = build_env(resolved)
        injected = inject(_declared_env(adapter), provided, self.vault)
        for value in (*provided.values(), *injected.values()):
            self.redactor.register(value)
        if injected:
            # the tool builds its own env at invoke time, so the injected values
            # travel with the parameters rather than in a local copy
            resolved[SECRET_ENV_KEY] = injected
        approval = self._resolve_approval(
            operation_id,
            definition,
            task_id,
            approval_id,
            approval_resolver,
            allowed_by_rule=decision is not None and decision.is_allowed,
            rule_label=(decision.rule.format() if decision is not None and decision.rule else ""),
        )
        redacted_params, _ = self._redact_params(validated)

        run = ToolRun(
            operation_id=operation_id,
            task_id=task_id,
            tool_id=definition.id,
            tool_version=definition.version,
            parameters=redacted_params,
            approval_id=approval.id,
        )
        action = self.recovery.begin_action(
            operation_id,
            action_type="tool.run",
            description=f"{definition.id} v{definition.version}",
            task_id=task_id,
            payload={
                "run_id": run.id,
                "tool_id": definition.id,
                "tool_version": definition.version,
            },
        )
        run.recovery_action_id = action.id
        self.runs.add_run(
            run,
            OperationEvent(
                operation_id=operation_id,
                event_type="tool.run.queued",
                payload={"run_id": run.id, "tool_id": definition.id},
            ),
        )

        expected_version = run.version
        # second line of defence: adapters check their own targets too, but a forgotten
        # check must not become a hole — the guard sees every host-shaped literal. It
        # runs once the run row exists, so a refusal is still visible in the history.
        try:
            guard(
                operation.scope,
                validated,
                exempt=tuple(getattr(adapter, "scope_exempt", ())),
            )
        except ScopeGuardError as exc:
            run.start()
            run.fail()
            self.runs.save_run(
                run,
                expected_version,
                OperationEvent(
                    operation_id=operation_id,
                    event_type="tool.run.failed",
                    payload={"run_id": run.id, "reason": str(exc)},
                ),
            )
            raise
        run.start()
        self.runs.save_run(
            run,
            expected_version,
            OperationEvent(
                operation_id=operation_id,
                event_type="tool.run.started",
                payload={"run_id": run.id, "tool_id": definition.id},
            ),
        )

        try:
            resolved["_run_id"] = run.id
            output = adapter.invoke(resolved, operation.scope)
        except ToolExecutionError:
            self._fail_run(run, action.id)
            raise
        except Exception as exc:  # adapter contract violation; fail closed
            self._fail_run(run, action.id)
            raise ToolExecutionError(f"tool {definition.id} failed: {exc}") from exc
        return self._finish_run(run, action.id, output)

    def _apply_relay(self, operation: Any, adapter: Any) -> None:
        """Point a sandbox adapter at the operation's relay, or back at docker."""
        from samurai_ai.execution.relay import RelaySandboxRunner, build_runner

        if not hasattr(adapter, "_runner"):
            return
        relay = operation.relay.strip()
        current = adapter._runner
        if relay and not isinstance(current, RelaySandboxRunner):
            adapter._runner = build_runner(relay)
        elif not relay and isinstance(current, RelaySandboxRunner):
            adapter._runner = None

    def rule_decision(
        self, definition: ToolDefinition, params: Mapping[str, Any]
    ) -> Decision | None:
        """The operator's answer for this call, when there is one.

        Only shells and commands carry a rule-able text; a tool without one keeps
        the old approval flow untouched.
        """
        if self.permission_rules is None:
            return None
        text = rule_text(params)
        if text is None:
            return None
        mode = parse_mode(self.permission_mode() if self.permission_mode else None)
        return decide(
            self.permission_rules(), definition.id, text, mode, classify_command(text)
        )

    def _resolve_approval(
        self,
        operation_id: str,
        definition: ToolDefinition,
        task_id: str | None,
        approval_id: str | None,
        resolver: Callable[[str], bool] | None = None,
        *,
        allowed_by_rule: bool = False,
        rule_label: str = "",
    ) -> Approval:
        action = f"tool.run {definition.id}"
        if approval_id is None:
            for existing in self.kernel.kernel.list_approvals(
                operation_id, ApprovalStatus.APPROVED
            ):
                if existing.action == action and existing.task_id == task_id:
                    return existing
            approval = self.kernel.request_approval(
                operation_id,
                action,
                definition.risk_class,
                task_id=task_id,
                reason=f"{definition.id} v{definition.version}",
                requested_by="operator",
            )
            if approval.status != ApprovalStatus.APPROVED and allowed_by_rule:
                return self.kernel.approve(approval.id, decided_by=f"rule:{rule_label}")
            if approval.status != ApprovalStatus.APPROVED:
                if resolver is None:
                    raise ApprovalRequiredError(approval.id)
                if not resolver(approval.id):
                    raise ToolDeniedError(
                        f"operator denied approval {approval.id} for {definition.id}"
                    )
                approval = self.kernel.approve(approval.id, decided_by="subagent")
            return approval
        stored = self.kernel.kernel.get_approval(approval_id)
        if stored is None:
            raise ApprovalNotFoundError(f"approval not found: {approval_id}")
        existing = stored
        if existing.operation_id != operation_id:
            raise ToolStateError(
                f"approval {approval_id} belongs to operation {existing.operation_id}"
            )
        if existing.action != action:
            raise ToolStateError(
                f"approval {approval_id} action {existing.action!r} does not match {action!r}"
            )
        if existing.task_id != task_id:
            raise ToolStateError(f"approval {approval_id} is bound to a different task")
        if existing.status is ApprovalStatus.PENDING:
            raise ApprovalRequiredError(approval_id)
        if existing.status is ApprovalStatus.REJECTED:
            raise ToolStateError(f"approval {approval_id} was rejected")
        return existing

    def _finish_run(self, run: ToolRun, action_id: str, output: ToolOutput) -> ToolResult:
        expected_version = run.version
        for secret in output.secrets:
            self.redactor.register(secret)
        stdout_result = self.redactor.redact(output.stdout)
        stderr_result = self.redactor.redact(output.stderr)
        redaction_count = stdout_result.count + stderr_result.count
        warnings = tuple(
            sorted(
                set(self.scanner.scan(stdout_result.text))
                | set(self.scanner.scan(stderr_result.text))
            )
        )

        if output.traffic:
            self._record_traffic(run, output)
        artifact_ids: list[str] = []
        stdout_artifact_id = self._register_output_artifact(
            run, stdout_result.text, is_stdout=True
        )
        if stdout_artifact_id is not None:
            artifact_ids.append(stdout_artifact_id)
        stderr_artifact_id = self._register_output_artifact(
            run, stderr_result.text, is_stdout=False
        )
        if stderr_artifact_id is not None:
            artifact_ids.append(stderr_artifact_id)

        if output.timed_out:
            run.mark_timeout(exit_code=output.exit_code)
            run.redaction_count = redaction_count
            run.injection_warnings = warnings
            run.stdout_artifact_id = stdout_artifact_id
            run.stderr_artifact_id = stderr_artifact_id
            event_type = "tool.run.timeout"
        elif output.exit_code == 0:
            run.complete(
                exit_code=output.exit_code,
                stdout_artifact_id=stdout_artifact_id,
                stderr_artifact_id=stderr_artifact_id,
                redaction_count=redaction_count,
                injection_warnings=warnings,
            )
            event_type = "tool.run.completed"
        else:
            run.fail(
                exit_code=output.exit_code,
                stdout_artifact_id=stdout_artifact_id,
                stderr_artifact_id=stderr_artifact_id,
                redaction_count=redaction_count,
                injection_warnings=warnings,
            )
            event_type = "tool.run.failed"
        payload: dict[str, Any] = {
            "run_id": run.id,
            "tool_id": run.tool_id,
            "exit_code": run.exit_code,
            "redaction_count": redaction_count,
            "injection_warnings": list(warnings),
        }
        if output.egress_events:
            payload["egress"] = list(output.egress_events)
        self.runs.save_run(
            run,
            expected_version,
            OperationEvent(
                operation_id=run.operation_id,
                event_type=event_type,
                payload=payload,
            ),
        )
        if run.status is ToolRunStatus.COMPLETED:
            self.recovery.complete_action(action_id)
        else:
            self.recovery.fail_action(action_id, reason=run.status.value)
        return ToolResult(
            run=run,
            stdout=stdout_result.text,
            stderr=stderr_result.text,
            artifact_ids=tuple(artifact_ids),
            trust=OutputTrust.UNTRUSTED_EXTERNAL,
            redaction_count=redaction_count,
            injection_warnings=warnings,
        )

    def _record_traffic(self, run: ToolRun, output: ToolOutput) -> None:
        """Evidence the adapter brought back; redacted, never fatal.

        A research read is only a citation if the pair exists, so a failure to
        record must not vanish quietly — it becomes a warning on the run.
        """
        if self.traffic is None:
            return
        for candidate in output.traffic:
            try:
                self.traffic.record(
                    run.operation_id,
                    str(candidate.get("method", "GET")),
                    str(candidate.get("url", "")),
                    int(candidate.get("status", 0)),
                    str(candidate.get("request_body", "")),
                    self.redactor.redact(str(candidate.get("response_body", ""))).text,
                    created_by=f"tool:{run.tool_id}",
                )
            except Exception:
                continue

    def _fail_run(self, run: ToolRun, action_id: str) -> None:
        expected_version = run.version
        run.fail()
        self.runs.save_run(
            run,
            expected_version,
            OperationEvent(
                operation_id=run.operation_id,
                event_type="tool.run.failed",
                payload={"run_id": run.id, "tool_id": run.tool_id, "exit_code": None},
            ),
        )
        self.recovery.fail_action(action_id, reason="error")

    def _register_output_artifact(
        self, run: ToolRun, content: str, *, is_stdout: bool
    ) -> str | None:
        if not content:
            return None
        artifact = self.kernel.register_artifact(
            run.operation_id,
            ArtifactKind.COMMAND_OUTPUT if is_stdout else ArtifactKind.LOG,
            content,
            summary=f"{run.tool_id} {'stdout' if is_stdout else 'stderr'} ({run.id})",
            task_id=run.task_id,
            created_by=f"tool:{run.tool_id}",
        )
        return artifact.id

    def _resolve_params(
        self,
        params: dict[str, Any],
        operation_id: str,
        optional: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        def walk(value: Any) -> Any:
            if isinstance(value, str):
                if not secret_references(value):
                    return value
                if self.vault is None:
                    raise ToolExecutionError(
                        "secret placeholders require a configured secrets vault"
                    )
                resolved, plaintexts = self.vault.resolve_placeholders(
                    value, operation_id
                )
                for plaintext in plaintexts:
                    self.redactor.register(plaintext)
                return resolved
            if isinstance(value, list):
                return [walk(item) for item in value]
            if isinstance(value, dict):
                return {key: walk(item) for key, item in value.items()}
            return value

        resolved_params: dict[str, Any] = {}
        for name, value in params.items():
            try:
                resolved_params[name] = walk(value)
            except (SecretNotFoundError, SecretScopeError):
                # an optional value (a key the operator has not set) simply goes
                # missing: the renderer then drops its argv entry altogether
                if name not in optional:
                    raise
        return resolved_params

    def _redact_params(self, params: dict[str, Any]) -> tuple[dict[str, Any], int]:
        count = 0

        def walk(value: Any) -> Any:
            nonlocal count
            if isinstance(value, str):
                result = self.redactor.redact(value)
                count += result.count
                return result.text
            if isinstance(value, list):
                return [walk(item) for item in value]
            if isinstance(value, dict):
                return {key: walk(item) for key, item in value.items()}
            return value

        return walk(params), count


def rule_text(params: Mapping[str, Any]) -> str | None:
    """The line an operator rule matches against.

    A shell tool carries its own command; everything else is described by its
    parameters, so a rule can still name the target that matters.
    """
    command = params.get("command")
    if isinstance(command, str) and command.strip():
        return command
    rendered = json.dumps(
        {key: value for key, value in params.items() if not str(key).startswith("_")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return rendered or None


def _optional_params(definition: object) -> frozenset[str]:
    """Manifest parameters whose absent value is legitimate."""
    params = getattr(definition, "parameters", None) or {}
    properties = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(properties, dict):
        return frozenset()
    return frozenset(
        name for name, spec in properties.items()
        if isinstance(spec, dict) and spec.get("optional")
    )


#: parameters key carrying env the executor injected from the vault
SECRET_ENV_KEY = "_secret_env"


def _declared_env(adapter: object) -> tuple[str, ...]:
    """Env names the adapter's profile allows; the vault may fill them."""
    profile_id = getattr(adapter, "profile_id", None)
    if not isinstance(profile_id, str):
        return ()
    try:
        from samurai_ai.execution.sandbox import get_profile

        return tuple(get_profile(profile_id).env_names)
    except Exception:  # noqa: BLE001 - an unknown profile just has no names
        return ()
