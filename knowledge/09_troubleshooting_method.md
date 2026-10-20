# A Safe Troubleshooting Method

1. Observe before acting. Collect evidence: which resource is saturated and which process is responsible.
2. Explain the cause in terms of OS concepts so the user understands what is happening.
3. Prefer reversible actions: lower priority (renice) or pause (SIGSTOP) before terminating.
4. Ask for confirmation before any action that can lose data (terminate, kill).
5. Never act on critical system processes or on processes owned by other users.
6. After every action, measure again. Compare before and after values to confirm the fix worked.
7. If the problem remains, try the next step; if it is solved, record the cause and the fix so the
   same problem can be fixed faster next time.
8. Record what was done in an action log for accountability.
