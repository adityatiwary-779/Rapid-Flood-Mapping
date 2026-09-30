# Deploying the full app (live custom dates)

**Not verified end to end.** These steps were written without access to Google Cloud, Upstash or Vercel; the code
paths were tested against mocks only. Run `tools/integration_check.py` after each part and paste any failure back.
Cloud Run, Cloud Tasks and Cloud Storage need a Google Cloud **billing account** (free-tier usage, but a card is required).

Just want the demo? Deploy `public/` alone (see "Static demo" at the end); no accounts needed.

## 0. What runs where
| Piece | Where | Needs |
|---|---|---|
| Website + thin API | Vercel (repo root) | env vars below |
| Pipeline worker (Earth Engine, roads) | Cloud Run | service account, bucket, Redis |
| Job queue | Cloud Tasks | queue |
| Results, presets | Cloud Storage | bucket |
| Job state, cache, rate limits | Upstash Redis | free account |

## 1. Google Cloud (run in Cloud Shell or any shell with `gcloud`)
```bash
PROJECT=YOUR_PROJECT_ID          # the project registered for Earth Engine
REGION=us-central1               # us-central1 keeps Cloud Storage inside its free tier
BUCKET=$PROJECT-flood-results
SA=flood-app@$PROJECT.iam.gserviceaccount.com
gcloud config set project $PROJECT

gcloud services enable earthengine.googleapis.com storage.googleapis.com run.googleapis.com \
  cloudtasks.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

# one service account for the API and the worker
gcloud iam service-accounts create flood-app --display-name "Flood app"
for ROLE in roles/earthengine.writer roles/serviceusage.serviceUsageConsumer roles/cloudtasks.enqueuer; do
  gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role $ROLE
done
gcloud iam service-accounts add-iam-policy-binding $SA --member serviceAccount:$SA --role roles/iam.serviceAccountUser
```
The project must be registered for Earth Engine (code.earthengine.google.com/register). If Earth Engine still
rejects the service account, check the current "service account" page in the Earth Engine docs.

**Bucket** (EE exports straight into it; public read is fine, results are not sensitive; alternatively set `SIGNED_URLS=1`
and skip the `allUsers` line):
```bash
gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member serviceAccount:$SA --role roles/storage.objectAdmin
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member allUsers --role roles/storage.objectViewer
gcloud storage buckets update gs://$BUCKET --cors-file=deploy/cors.json      # lets the browser load roads/settlement layers
```
**Key** (used as an env var; never commit it, then delete the local file):
```bash
gcloud iam service-accounts keys create key.json --iam-account $SA
base64 -w0 key.json > key.b64        # macOS: base64 -i key.json -o key.b64      Windows: certutil -encode key.json key.b64
```
If key creation is blocked by an organisation policy, tell me: the alternative is workload identity, which changes the code.

**Queue:**
```bash
gcloud tasks queues create flood-jobs --location=$REGION --max-attempts=3 --min-backoff=30s \
  --max-dispatches-per-second=2 --max-concurrent-dispatches=2
```

## 2. Upstash Redis
Create a free Redis database at upstash.com (or via the Vercel Marketplace). Copy the **REST URL** and **REST token**.

## 3. Cloud Run worker
```bash
gcloud artifacts repositories create flood --repository-format=docker --location=$REGION
IMAGE=$REGION-docker.pkg.dev/$PROJECT/flood/worker
gcloud builds submit --config deploy/cloudbuild.yaml --substitutions _IMAGE=$IMAGE .

gcloud secrets create flood-sa-key --data-file=key.b64
gcloud secrets create flood-redis-token --data-file=<(printf '%s' 'PASTE_UPSTASH_REST_TOKEN')
gcloud secrets add-iam-policy-binding flood-sa-key --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding flood-redis-token --member serviceAccount:$SA --role roles/secretmanager.secretAccessor

gcloud run deploy flood-worker --image $IMAGE --region $REGION --service-account $SA \
  --no-allow-unauthenticated --memory 2Gi --cpu 1 --timeout 900 --concurrency 4 --max-instances 3 \
  --set-secrets EE_SERVICE_ACCOUNT_JSON=flood-sa-key:latest,UPSTASH_REDIS_REST_TOKEN=flood-redis-token:latest \
  --set-env-vars BACKEND_MODE=real,EE_PROJECT=$PROJECT,GCS_BUCKET=$BUCKET,TASKS_PROJECT=$PROJECT,TASKS_LOCATION=$REGION,TASKS_QUEUE=flood-jobs,TASKS_INVOKER_SA=$SA,UPSTASH_REDIS_REST_URL=PASTE_UPSTASH_REST_URL,IP_SALT=$(openssl rand -hex 16)
WORKER_URL=$(gcloud run services describe flood-worker --region $REGION --format='value(status.url)')
gcloud run services update flood-worker --region $REGION --update-env-vars WORKER_URL=$WORKER_URL
gcloud run services add-iam-policy-binding flood-worker --region $REGION --member serviceAccount:$SA --role roles/run.invoker
```

## 4. Check the services from your machine
```bash
pip install -r requirements-dev.txt
export BACKEND_MODE=real EE_PROJECT=... GCS_BUCKET=... WORKER_URL=... TASKS_INVOKER_SA=$SA TASKS_LOCATION=$REGION TASKS_QUEUE=flood-jobs \
       UPSTASH_REDIS_REST_URL=... UPSTASH_REDIS_REST_TOKEN=... EE_SERVICE_ACCOUNT_JSON="$(cat key.b64)"
python tools/integration_check.py services       # paste the report back if anything says FAIL
```
It checks: env vars, key, Earth Engine login and datasets, bucket read/write/CORS, Redis, worker reachability, Cloud Tasks,
a real preflight (and its **speed**, see "Function time limit"), and that every preset district name exists.

## 5. Real district list and preset events
```bash
python tools/build_regions.py --project $PROJECT       # writes data/regions.json, checks preset spellings; commit the file
python tools/precompute_presets.py                     # builds Kerala / Assam / Chennai into gs://$BUCKET/presets (minutes each)
```
If a district name is wrong, fix it in `data/presets.json` using the spelling `build_regions.py` prints.
The Assam and Chennai districts in `data/presets.json` are my guesses.

## 6. Vercel (full app)
Import the repo, **Root Directory = repo root** (`.`), Framework **Other**, no build command. Environment variables
(Production): `BACKEND_MODE=real`, `EE_PROJECT`, `EE_SERVICE_ACCOUNT_JSON` (contents of `key.b64`), `GCS_BUCKET`,
`UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, `TASKS_PROJECT`, `TASKS_LOCATION`, `TASKS_QUEUE`, `WORKER_URL`,
`TASKS_INVOKER_SA`, `IP_SALT` (any random string). Mark the key and token as Sensitive. Deploy, then:
```bash
python tools/integration_check.py api --url https://YOUR-APP.vercel.app              # presets, validation, preflight
python tools/integration_check.py api --url https://YOUR-APP.vercel.app --run-job    # one real analysis end to end
```

## Function time limit (Vercel)
`POST /api/preflight` and `POST /api/jobs` make one Earth Engine round trip (cached for 6 h). The integration check prints how
many seconds it takes. If it is close to your plan's function limit, either raise `maxDuration` for `api/index.py` in
`vercel.json` (only up to what your plan allows: check Vercel's current limits page), or ask me to pre-compute image counts.

## Limits of the free tiers (check current numbers before relying on them)
Earth Engine has monthly compute quotas per project and limits on concurrent tasks; districts (<= 5,000 km2) are small,
but many users will exhaust the quota, hence the daily job cap (`GLOBAL_JOBS_PER_DAY`, default 30). Vercel Hobby is for
non-commercial use. Cloud Run, Cloud Tasks and Upstash have free monthly allowances that this app should stay within.

## Static demo (no accounts)
`public/` is a complete static site. On Vercel set **Root Directory = public**; no env vars. Regenerate with
`python tools/build_static_demo.py`.
