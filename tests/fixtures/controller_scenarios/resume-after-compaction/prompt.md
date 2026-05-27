Simulated post-compaction wake: you have **no prior chat history**. Your only
handoff artifact is the resume notes file:

{{RESUME_NOTES_PATH}}

Follow Goal Flight resume protocol (`commands/resume.md` and `protocols/state-handoff.md`):

1. Read the resume notes file above.
2. Run:
   python3 scripts/goalflight_status.py --json
3. Run:
   git status --short
   git log -1 --oneline
4. Run this fast test subset to verify the repo still passes after compaction handoff:
   python3 tests/python/test_controller_probe_matrix.py
   python3 tests/python/test_compaction_resume_drill.py

Working directory: {{PROJECT_ROOT}}

In your reply (captured in a bash-tail log), briefly state:
- that you read the resume notes
- whether status JSON returned successfully
- whether both test scripts passed

End with a line exactly: COMPLETE: true

Do not edit repository files. Do not start new feature work beyond this verification.
