# Evaluation harness

## Run privately

Use an authorized target supplied through local environment variables. Keep all
results, logs and evidence outside public git.

```bash
TARGET_IP=<authorized-target>
samurai eval bench evals/bench/takeoff.json \
  --host "$TARGET_IP" \
  --relay "ssh -F ~/.ssh/lab.conf vm"
```

The suite finishes only after all hypotheses are closed and a final conclusion is
recorded. Capture local evidence success, terminal correctness, blocked reasons,
wall time, cost and safety violations. Do not publish those artifacts.

## Local acceptance

- Use real HTB and Linux services, not mocked tool output.
- Include RDP, CUPS, scope expansion, isolation and reconnect scenarios.
- Repeat with unstable networking and require safe `UNKNOWN` outcomes instead of
  replaying an unobserved remote action.
- Keep the resulting evidence local and redact secrets before sharing any report.
