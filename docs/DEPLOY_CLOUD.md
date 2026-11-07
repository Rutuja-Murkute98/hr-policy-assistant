# Optional: running on AWS or GCP

[DEPLOY.md](../DEPLOY.md) puts the helpdesk on free services. Use this guide if you outgrow them: for example, if you need
the API to stay awake, a private network, or a region of your choice for real data. No Kubernetes is needed.

> **None of this has been run.** There's no AWS or GCP account behind this guide, and the Docker images haven't been built
> on this PC. Treat it as a starting plan and test each step. Services and prices change, so check the current docs. This
> costs money, unlike the free stack.

## What you need in either cloud

| Part | What it does | Notes |
|---|---|---|
| API container | `Dockerfile.api` | At least 512 MB of memory. It stayed under 260 MB here, but keep a margin for large uploads |
| Chat screen container | `Dockerfile.ui` | Or keep it on Streamlit Community Cloud |
| PostgreSQL 16 or newer with pgvector | The database | The user must be able to create a role: see `DEPLOY.md` |
| Secret storage | Model keys, database address and client secrets | Never in images or the repository |
| HTTPS | In front of the API and chat screen | Sign-in needs it |

Set these on the API, as well as the settings in `.env.example`:

```text
AUTH_MODE=oidc
OIDC_ISSUER=...
OIDC_CLIENT_ID=...
PLATFORM_ADMIN_EMAILS=...
LOG_FORMAT=json
```

**Use one API process per small container, and add containers to scale.** Rate limits and paused approvals are in the
database, so they work across containers. Indexing jobs run inside the API process, and a job that was running when a
container restarted is marked as failed, so it can be retried. If you run several containers, a starting container can
mark another's running job as failed, and it then needs a retry. A real job queue would fix that.

For real employee data, read [the DPDP notes](DPDP_NOTES.md) first, and choose a region and model provider deliberately.

## GCP: Cloud Run and Cloud SQL

1. Create a Cloud SQL for PostgreSQL instance, and turn on the `vector` extension.
2. Create an Artifact Registry repository, then build and push the images:
   ```bash
   gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/helpdesk/api --file Dockerfile.api .
   ```
3. Store secrets in Secret Manager.
4. Deploy the API, connecting to Cloud SQL and reading the secrets:
   ```bash
   gcloud run deploy helpdesk-api --image REGION-docker.pkg.dev/PROJECT/helpdesk/api --memory 1Gi --port 8000 --add-cloudsql-instances PROJECT:REGION:INSTANCE --set-secrets GOOGLE_API_KEY=google-key:latest,DATABASE_URL=database-url:latest
   ```
5. Set `--min-instances 1` if you don't want a cold start on the first question. It costs more. Cloud Run may also limit CPU
   between requests, which can stall an indexing job running in the background, so turn on always-allocated CPU.
6. Deploy the chat screen the same way, with `HELPDESK_API_URL` set to the API's address.
7. Index the policies once from your PC, with `DATABASE_URL` pointing at the new database.

## AWS: ECS Fargate and RDS

1. Create an RDS for PostgreSQL instance, and run `CREATE EXTENSION vector`.
2. Create ECR repositories, then build and push `Dockerfile.api` and `Dockerfile.ui`.
3. Store secrets in Secrets Manager.
4. Create an ECS cluster on Fargate, with a task for the API of at least 0.5 vCPU and 1 GB. Refer to the secrets in the
   task definition, and send logs to CloudWatch.
5. Put an Application Load Balancer with an ACM certificate in front. Use `/live` for the health check, as `/health`
   touches the database.
6. Keep RDS in private subnets, reachable only from the API's security group.
7. Index the policies once, using a one-off task or a machine that can reach the database.

## After deploying

1. Call `/health`, and check `chunks_indexed`.
2. Sign in, and add people on the admin screen.
3. Run the load test against a **test** deployment, not one in use: see `loadtest/`. Results from the free stack or a PC
   don't carry over to a new setup.
4. Test a backup and restore: see [OPERATIONS.md](OPERATIONS.md). Managed databases also have their own automated backups,
   which you should turn on.
