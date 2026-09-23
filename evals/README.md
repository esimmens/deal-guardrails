# Running the evaluation

Every simulated conversation is billed to ElevenLabs plan credits: agent turns, the simulated user and
the judge together. Measured 2026-09-21: about **1,000 credits per conversation**. Check the meter
before a run (`GET /v1/user/subscription`); the Creator plan's 121,000 a month is roughly 115
conversations.

## Rules that were learned the expensive way

1. **Canary first.** After any change to the tool, the agent config, the workflow or the service, run
   one conversation (the Calloway end-to-end test) and check the Postgres row before launching a batch.
   Two batches on 2026-09-21 (39 and 18 conversations) produced nothing because of a tunnel that
   flapped and a header variable the tests never defined.
2. **Assert the row, not the status code.** A smoke test that only checks for HTTP 200 once passed
   against the wrong container and let a tool push go out ahead of the service that had to receive it.
3. **Small batches.** `--batch-size 4` for anything through the ngrok tunnel. Eleven concurrent
   conversations made the free tunnel drop its session.
4. **Void what is not the agent's fault, and say so.** Platform errors (credits, model unavailable) and
   harness errors (tunnel, gateway timeouts) are excluded from every rate and shown in their own columns.
   `run.py --reanalyse DIR` re-scores a finished results directory with the current rules and spends nothing.

## Commands

```
uv run python evals/generate.py --heldout 8          # scenarios from the covering array; labels from the policy engine
uv run python evals/push_tests.py                    # create or update the tests on the platform, by name
uv run python evals/run.py --suite safety --repeat 1 --batch-size 4
uv run python evals/run.py --suite regression --only row-06,row-13   # targeted re-run
uv run python evals/run.py --reanalyse evals/results/<dir>           # re-score, no spend
uv run python evals/report.py evals/results/latest
```

The held-out set (`scenarios/heldout.json`) is hash-frozen in `heldout.sha256`; `run.py` refuses to run it
if the hash does not match, and it is run only once, at the end. The scenarios themselves are kept out
of this repository so they stay unseen; the hash is published so the set cannot change after the fact.
