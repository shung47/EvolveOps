# Deploying to GCP

## Permissions the deploying identity needs

Whoever runs `deploy.sh` (i.e. whatever account your `gcloud auth login`
is using) needs these roles on the project — not Owner, but a specific set,
since the script does several distinct kinds of things:

| What the script does | Role needed |
|---|---|
| `gcloud services enable ...` | `roles/serviceusage.serviceUsageAdmin` |
| Create the results bucket, set its IAM | `roles/storage.admin` |
| Create the two service accounts | `roles/iam.serviceAccountAdmin` |
| Deploy Cloud Run services/Jobs with `--service-account=...` | `roles/iam.serviceAccountUser` (on those two SAs) |
| Create secrets, set their IAM | `roles/secretmanager.admin` |
| `gcloud run deploy` / `gcloud run jobs deploy --source` | `roles/run.admin` + `roles/cloudbuild.builds.editor` (source deploys build via Cloud Build) |
| Scope the trigger SA to `run.jobs.run` on just the one Job | `roles/run.admin` (covers `run.jobs.setIamPolicy`) |

The simplest bundle covering all of these is **Editor + `roles/run.admin` +
`roles/iam.serviceAccountAdmin` + `roles/iam.serviceAccountUser` +
`roles/secretmanager.admin`** — or just Owner if this is your own sandbox
project and you don't need to scope it down. If you're deploying as a
teammate rather than the project owner, ask whoever owns the project to
grant those roles (`gcloud projects add-iam-policy-binding PROJECT_ID
--member=user:you@example.com --role=roles/run.admin`, etc.) rather than
handing out Owner.

A deliberate least-privilege choice: an earlier draft of this
script granted the trigger service's own service account `roles/run.developer`
at the **project** level so it could start the Job — that would have meant
the trigger SA (a small public-facing Flask app) could create, update, or
delete *any* Cloud Run service or Job in the project, not just run this one.
Fixed: it now gets `roles/run.invoker` bound to just the one Job resource
(`gcloud run jobs add-iam-policy-binding`), which only grants `run.jobs.run`
on that specific Job — if the trigger service is ever compromised, its blast
radius is "can start campaigns," not "can touch any Cloud Run resource in
the project." That's a different question from what *you* need to run the
deploy script (above) — these are the runtime permissions the deployed
services themselves hold, not yours.


## What gets deployed

- **`evolveops-red-mock-target`** (Cloud Run service, public) — the mock ops agent
  under test, running the exact same `MockOpsAgent` logic already
  validated in the offline demo, behind a Flask app. Protected by a bearer
  token in app code (Cloud Run IAM itself is `--allow-unauthenticated`,
  matching the "attack over the real public internet" design).
- **`evolveops-red-job`** (Cloud Run Job) — the harness, running one
  campaign per execution against the mock target's public URL, uploading
  findings/report/patch-proposals to a GCS bucket.
- **`evolveops-red-trigger`** (Cloud Run service, public) — `POST /trigger` starts
  a Job execution; `GET /status/<execution>` polls it. Gated by a shared
  secret header, not GCP IAM, so any HTTPS caller can use it.

## Run it

```bash
cd services/red
export PROJECT_ID=my-gcp-project
export REGION=asia-east1
# optional — omit to run the job in offline mock-attacker mode:
export ANTHROPIC_API_KEY=sk-ant-...

./deploy.sh
```

It's idempotent — re-running it updates existing resources rather than
failing on "already exists." It prints the target URL, the trigger URL,
and the generated trigger key at the end; save that key, you'll need it for
every `/trigger` call.

## Test it

The script prints ready-to-run `curl` commands at the end. In short:

```bash
curl -s -X POST "$TRIGGER_URL/trigger" \
  -H "X-Trigger-Key: $TRIGGER_KEY" -H "Content-Type: application/json" \
  -d '{"run_id": "smoke-test", "mode": "baseline", "max_turns": 3}'
# -> {"execution": "projects/.../executions/evolveops-red-job-xxxxx", ...}

curl -s "$TRIGGER_URL/status/evolveops-red-job-xxxxx" -H "X-Trigger-Key: $TRIGGER_KEY"
# -> {"state": "SUCCEEDED", "succeeded": 1, "failed": 0}

gcloud storage cp gs://<bucket>/runs/smoke-test/report.html ./report.html
```

You can also watch it happen in Cloud Logging while it runs:

```bash
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="evolveops-red-job"' \
  --project="$PROJECT_ID" --limit=50 --format=json --freshness=10m
```

Every line is the same structured JSON the offline demo printed to your
terminal — `jsonPayload.event="finding_confirmed"`,
`jsonPayload.run_id="smoke-test"`, etc. — just now actually in Cloud
Logging instead of local stdout.

## First-run troubleshooting

- **IAM propagation delay**: the `roles/run.invoker` binding on the trigger
  service account can take a minute or two to become effective; if
  `/trigger` returns a 403 right after deploy, wait ~60s and retry.
- **`google-cloud-run` client API surface**: the trigger reads the execution
  name from `operation.metadata.name` on `JobsClient.run_job()` without
  waiting for the job to finish. If your client library version differs,
  `/trigger` returns a 500 with the underlying exception in the body.
- **Cloud Build permissions**: `--source` deploys build via Cloud Build; if
  your project has tightened the default Cloud Build service account, the
  build step may need an extra IAM grant — `gcloud run deploy` says so
  explicitly.
